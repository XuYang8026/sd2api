"""The pool must route jobs to subaccounts that can pay for them.

Creative Studio charges ``duration × creditsPerSecond`` when a task is created
and answers ``10044200 InsufficientCredits`` when the selected subaccount can't
cover it. The scheduler should avoid subaccounts whose known balance is too low
and, when its stored balance is stale, fail over instead of surfacing the error.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from sd2api.browser_pool import BrowserPoolClient
from sd2api.config import Settings
from sd2api.credits import estimated_job_credits
from sd2api.store import TaskStore
from sd2api.tiktok import TikTokUpstreamError, is_insufficient_credits_error
from sd2api.uploads import StagedMedia


def insufficient_credits() -> TikTokUpstreamError:
    # The exact error the protocol client raises for TikTok's HTTP-200
    # business failure seen in production on 2026-11-07.
    return TikTokUpstreamError("InsufficientCredits", status_code=502, code="10044200")


@pytest.mark.parametrize(
    ("model", "duration", "expected"),
    [
        ("seedance-2.0", 11, 55),
        ("sora-2", 4, 20),
        ("seedance-2.5", 6, 60),
        ("seedance-2.0-fast", 11, 33),
        ("Seedance-2.0-Fast", 11, 33),
        ("seedance_2_0_mini", 4, 8),
        ("dreamina-seedance-2.0-mini", 15, 30),
        ("unknown-model", 10, None),
    ],
)
def test_estimated_job_credits_follows_the_studio_rate_card(
    model: str, duration: int, expected: int | None
) -> None:
    assert estimated_job_credits(model, duration) == expected


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (insufficient_credits(), True),
        (TikTokUpstreamError("InsufficientCredits", code="tiktok_http_400"), True),
        (TikTokUpstreamError("Insufficient credits to generate", code="1"), True),
        (TikTokUpstreamError("积分不足", code="1"), True),
        (TikTokUpstreamError("Daily generation limit reached", code="daily_limit"), False),
        (TikTokUpstreamError("没有模型使用权限", status_code=403, code="10001100"), False),
    ],
)
def test_insufficient_credits_error_is_recognized(
    error: TikTokUpstreamError, expected: bool
) -> None:
    assert is_insufficient_credits_error(error) is expected


class FakeProtocol:
    """A protocol client that either accepts tasks or reports a low balance."""

    def __init__(self, name: str, *, load: int = 0, broke: bool = False) -> None:
        self.name = name
        self.load = load
        self.broke = broke
        self.calls = 0
        self.seen_media_present: list[bool] = []

    async def _create(self, media: list[StagedMedia] | None = None) -> str:
        self.calls += 1
        self.seen_media_present.extend(Path(item.path).exists() for item in media or [])
        if self.broke:
            raise insufficient_credits()
        self.load += 1
        return f"{self.name}-task-{self.calls}"

    async def create_text_video(self, **kwargs: Any) -> str:
        return await self._create()

    async def create_reference_video(self, **kwargs: Any) -> str:
        return await self._create(kwargs["media"])


def build_pool(
    tmp_path: Path,
    subaccounts: dict[str, int | None],
    clients: dict[str, FakeProtocol],
) -> tuple[BrowserPoolClient, TaskStore]:
    """One logged-in protocol account per subaccount, with the given credits."""
    store = TaskStore(str(tmp_path / "credits.db"))
    for advertiser_id, credits in subaccounts.items():
        account_id = f"login-{advertiser_id}"
        store.create_account(account_id=account_id, name=account_id)
        store.upsert_subaccounts(
            account_id,
            [
                {
                    "advertiser_id": advertiser_id,
                    "name": advertiser_id,
                    "account_type": "partner",
                    "seedance_access": True,
                    "credits": credits,
                }
            ],
        )
        store.set_subaccount_enabled(account_id, advertiser_id, True)
    pool = BrowserPoolClient(
        Settings(
            sd2api_pool_subaccount_concurrency=5,
            sd2api_pool_max_pending=500,
            sd2api_upload_dir=str(tmp_path / "uploads"),
        ),
        store,
    )

    async def fake_list_accounts() -> list[dict[str, Any]]:
        return [
            {
                "id": f"login-{advertiser_id}",
                "enabled": True,
                "running": True,
                "logged_in": True,
                "session_available": True,
                "subaccounts": pool._decorate_subaccounts(
                    f"login-{advertiser_id}",
                    store.list_subaccounts(f"login-{advertiser_id}"),
                ),
            }
            for advertiser_id in subaccounts
        ]

    pool.list_accounts = fake_list_accounts  # type: ignore[method-assign]
    pool._protocol_client = (  # type: ignore[method-assign]
        lambda _account_id, advertiser_id=None: clients[str(advertiser_id)]
    )
    return pool, store


def stored_credits(store: TaskStore, advertiser_id: str) -> int | None:
    (item,) = store.list_subaccounts(f"login-{advertiser_id}")
    return item["credits"]


async def test_pool_fails_over_when_upstream_reports_insufficient_credits(
    tmp_path: Path,
) -> None:
    # The stored balance of sub-stale is out of date, so it looks like the
    # best choice until TikTok refuses the 33-credit Fast job.
    clients = {
        "sub-stale": FakeProtocol("sub-stale", broke=True),
        "sub-funded": FakeProtocol("sub-funded"),
    }
    pool, store = build_pool(
        tmp_path, {"sub-stale": 5000, "sub-funded": 2296}, clients
    )
    upload_dir = tmp_path / "uploads"
    upload_dir.mkdir()
    image = upload_dir / "reference.png"
    image.write_bytes(b"png")

    task_id = await pool.create_reference_video(
        prompt="take C",
        model="seedance-2.0-fast",
        duration=11,
        media=[StagedMedia(kind="image", path=str(image))],
    )

    assert task_id == "sub-funded-task-1"
    assert pool.account_for_task(task_id) == "login-sub-funded"
    assert clients["sub-stale"].calls == 1
    # The staged reference survives the failed attempt for the retry.
    assert clients["sub-funded"].seen_media_present == [True]
    # The refused subaccount no longer looks able to pay for this job.
    assert stored_credits(store, "sub-stale") < 33
    assert stored_credits(store, "sub-funded") == 2296
    events = store.list_events(search="sub-stale")
    assert any("credits" in event.message.lower() for event in events)


@pytest.mark.parametrize("funded_credits", [2296, None])
async def test_pool_skips_subaccount_whose_known_credits_cannot_cover_job(
    tmp_path: Path, funded_credits: int | None
) -> None:
    # Production state on 2026-11-07: the idle subaccount had 17 credits while
    # the busier ones had thousands; an 11 s Fast job needs 33.
    clients = {
        "sub-low": FakeProtocol("sub-low"),
        "sub-funded": FakeProtocol("sub-funded", load=1),
    }
    pool, _store = build_pool(
        tmp_path, {"sub-low": 17, "sub-funded": funded_credits}, clients
    )

    task_id = await pool.create_text_video(
        prompt="skip the poor one", model="seedance-2.0", duration=11
    )

    assert task_id == "sub-funded-task-1"
    assert clients["sub-low"].calls == 0


async def test_pool_still_uses_low_credit_subaccount_for_a_cheap_job(
    tmp_path: Path,
) -> None:
    clients = {
        "sub-low": FakeProtocol("sub-low"),
        "sub-funded": FakeProtocol("sub-funded", load=1),
    }
    pool, _store = build_pool(
        tmp_path, {"sub-low": 17, "sub-funded": 2296}, clients
    )
    upload_dir = tmp_path / "uploads"
    upload_dir.mkdir()
    image = upload_dir / "reference.png"
    image.write_bytes(b"png")

    # A 4 s Mini job costs 8 credits, which the idle subaccount can afford.
    task_id = await pool.create_reference_video(
        prompt="cheap",
        model="seedance-2.0-mini",
        duration=4,
        media=[StagedMedia(kind="image", path=str(image))],
    )

    assert task_id == "sub-low-task-1"


async def test_pool_tries_low_credit_subaccounts_when_none_can_cover_job(
    tmp_path: Path,
) -> None:
    # Stored balances can be stale (e.g. right after the weekly refresh), so
    # when nobody looks able to pay, the pool still asks TikTok.
    clients = {"sub-low": FakeProtocol("sub-low")}
    pool, _store = build_pool(tmp_path, {"sub-low": 17}, clients)

    task_id = await pool.create_text_video(
        prompt="stale balance", model="seedance-2.0", duration=11
    )

    assert task_id == "sub-low-task-1"


async def test_pool_waits_for_funded_slot_instead_of_using_low_credit_subaccount(
    tmp_path: Path,
) -> None:
    clients = {
        "sub-low": FakeProtocol("sub-low"),
        "sub-funded": FakeProtocol("sub-funded", load=5),
    }
    pool, _store = build_pool(
        tmp_path, {"sub-low": 17, "sub-funded": 2296}, clients
    )

    with pytest.raises(TikTokUpstreamError) as error:
        await pool.create_text_video(
            prompt="funded one is busy", model="seedance-2.0", duration=11
        )

    assert error.value.status_code == 429
    assert error.value.code == "subaccount_concurrency_full"
    assert clients["sub-low"].calls == 0


async def test_pool_returns_402_when_every_candidate_lacks_credits(
    tmp_path: Path,
) -> None:
    clients = {
        "sub-a": FakeProtocol("sub-a", broke=True),
        "sub-b": FakeProtocol("sub-b", broke=True),
    }
    pool, store = build_pool(tmp_path, {"sub-a": 900, "sub-b": None}, clients)

    with pytest.raises(TikTokUpstreamError) as error:
        await pool.create_text_video(
            prompt="everyone is broke", model="seedance-2.0", duration=11
        )

    assert error.value.status_code == 402
    assert error.value.code == "insufficient_credits"
    assert "55" in str(error.value)
    assert clients["sub-a"].calls == clients["sub-b"].calls == 1
    assert stored_credits(store, "sub-a") < 55
    assert stored_credits(store, "sub-b") < 55
