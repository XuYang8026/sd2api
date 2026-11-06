from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from sd2api.browser_pool import BrowserPoolClient
from sd2api.queue_wait import (
    QUEUE_WAIT_PATH,
    QueueWaitEstimator,
    QueueWaitService,
    QueueWaitUnavailable,
    SubmissionLog,
    describe,
    parse_wait_estimates,
    raw_strategy,
    strategy_for,
)
from sd2api.store import TaskRecord, TaskStore

# Shape captured from Creative Studio on 2026-11-04.
LIVE_PAYLOAD = {
    "BaseResp": {"StatusCode": 0, "StatusMessage": ""},
    "results": [
        {"queues": [{"estimated_wait_seconds": "480", "queue_alias": "P1"}], "strategy_id": "3000014"},
        {
            "queues": [
                {"estimated_wait_seconds": "20276", "queue_alias": "H1"},
                {"estimated_wait_seconds": "37398", "queue_alias": "LH1"},
            ],
            "strategy_id": "2000004",
        },
    ],
}


class Clock:
    def __init__(self, now: float = 1_000_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


def record(task_id: str = "task-1", *, status: str = "running", created_at: int = 1_000_000, **extra: Any) -> TaskRecord:
    return TaskRecord(
        id=task_id, api="seedance", model="seedance-2.0", prompt="p", seconds=15, size="720x1280",
        ratio="adaptive", resolution="720p", status=status, progress=50,
        created_at=created_at, updated_at=created_at, **extra,
    )


def test_parse_takes_the_longest_queue_like_creative_studio() -> None:
    payload = {
        "results": [
            *LIVE_PAYLOAD["results"],
            {"strategy_id": "5000005", "queues": [{"estimated_wait_seconds": "-1"}, {"estimated_wait_seconds": "abc"}, {}]},
            {"strategy_id": "4000005", "queues": []},
            "not-a-result",
        ]
    }

    assert parse_wait_estimates(payload) == {"3000014": 480, "2000004": 37398}
    assert parse_wait_estimates({}) == {}


@pytest.mark.parametrize(
    ("mode", "model", "expected"),
    [
        ("text", "seedance-2.0", "5000005"),
        ("image", "seedance-2.0", "4000005"),
        ("reference", "seedance-2.0", "2000004"),
        ("reference", " Seedance-2.5 ", "2000008"),
        ("text", "sora-2", "5000005"),
        ("first_last", "seedance-2.0", None),
        ("text", "unknown-model", None),
    ],
)
def test_strategy_for_maps_mode_and_model(mode: str, model: str, expected: str | None) -> None:
    assert strategy_for(mode, model) == expected


async def test_estimator_caches_for_ttl_and_only_fetches_stale_strategies() -> None:
    clock = Clock()
    calls: list[list[str]] = []

    async def fetch(strategy_ids: list[str]) -> dict[str, Any]:
        calls.append(strategy_ids)
        return LIVE_PAYLOAD

    estimator = QueueWaitEstimator(fetch, ttl=90, clock=clock)

    first = await estimator.estimates(["2000004", None, "2000004"])
    clock.now += 30
    await estimator.estimates(["2000004"])
    await estimator.estimates(["2000004", "3000014"])
    clock.now += 61
    await estimator.estimates(["2000004", "3000014"])

    assert first == {"2000004": {"seconds": 37398, "checked_at": 1_000_000}}
    assert calls == [["2000004"], ["3000014"], ["2000004"]]
    assert estimator.fresh("3000014") == (True, 480)
    clock.now += 90
    assert estimator.fresh("3000014") == (False, None)


async def test_estimator_remembers_strategies_without_queue_info() -> None:
    calls: list[list[str]] = []

    async def fetch(strategy_ids: list[str]) -> dict[str, Any]:
        calls.append(strategy_ids)
        return {"results": []}

    estimator = QueueWaitEstimator(fetch, clock=Clock())

    assert await estimator.estimates(["2000008"]) == {}
    assert await estimator.estimates(["2000008"]) == {}
    assert calls == [["2000008"]]
    assert estimator.fresh("2000008") == (True, None)


async def test_estimator_serves_last_values_and_backs_off_after_errors() -> None:
    clock = Clock()
    calls = 0

    async def fetch(strategy_ids: list[str]) -> dict[str, Any]:
        nonlocal calls
        calls += 1
        if calls > 1:
            raise RuntimeError("TikTok unavailable")
        return LIVE_PAYLOAD

    estimator = QueueWaitEstimator(fetch, ttl=90, max_age=600, clock=clock)
    await estimator.estimates(["2000004"])

    clock.now += 100
    after_error = await estimator.estimates(["2000004"])
    clock.now += 30
    during_backoff = await estimator.estimates(["2000004"])
    clock.now += 600
    too_old = await estimator.estimates(["2000004"])

    assert after_error == {"2000004": {"seconds": 37398, "checked_at": 1_000_000}}
    assert during_backoff == after_error
    assert calls == 3  # first load, failure at +100, retry after the back-off at +730
    assert too_old == {}


def test_describe_reports_remaining_overdue_and_progress() -> None:
    task = record(created_at=1_000)
    current = {"seconds": 37398, "checked_at": 4_000}

    halfway = describe(task, now=4_600, strategy_id="2000004", submitted_wait=7_200, current=current)
    overdue = describe(task, now=9_000, strategy_id="2000004", submitted_wait=7_200, current=None)
    unknown = describe(task, now=1_500, strategy_id=None, submitted_wait=None, current=None)
    no_wait = describe(task, now=1_010, strategy_id="5000005", submitted_wait=0, current=None)

    assert halfway == {
        "strategy_id": "2000004", "elapsed_seconds": 3_600, "current_wait_seconds": 37398,
        "checked_at": 4_000, "submit_wait_seconds": 7_200, "remaining_seconds": 3_600,
        "overdue_seconds": 0, "progress": 50,
    }
    assert overdue["remaining_seconds"] == 0
    assert overdue["overdue_seconds"] == 800
    assert overdue["progress"] == 95
    assert unknown == {"strategy_id": None, "elapsed_seconds": 500}
    assert no_wait["remaining_seconds"] == 0 and "progress" not in no_wait
    assert describe(task, now=1_001, strategy_id="x", submitted_wait=7_200, current=None)["progress"] == 1


def test_raw_strategy_reads_the_matching_draft() -> None:
    raw = {"data": {"draft_infos": [{"taskId": "other", "strategyId": "5000005"}, {"taskId": "task-1", "strategyId": "2000004"}]}}

    assert raw_strategy(record(raw=raw)) == "2000004"
    assert raw_strategy(record(raw={"data": {"draft_infos": [{"strategyId": "4000005"}]}})) == "4000005"
    assert raw_strategy(record(raw={"code": 0})) is None
    assert raw_strategy(record()) is None


def test_submission_log_round_trip(tmp_path: Path) -> None:
    path = str(tmp_path / "tasks.db")
    TaskStore(path)
    log = SubmissionLog(path)

    log.record("a", "2000004", 37398, 100)
    log.record("b", "5000005", None, 101)
    log.record("a", "2000004", 120, 102)

    assert log.get_many(["a", "b", "missing", "a"]) == {
        "a": {"task_id": "a", "strategy_id": "2000004", "wait_seconds": 120, "recorded_at": 102},
        "b": {"task_id": "b", "strategy_id": "5000005", "wait_seconds": None, "recorded_at": 101},
    }
    assert SubmissionLog(path).get_many([]) == {}


class FakeProtocol:
    def __init__(self) -> None:
        self.requests: list[tuple[str, str, dict[str, Any]]] = []

    async def _request(self, method: str, path: str, *, json_body: dict[str, Any]) -> dict[str, Any]:
        self.requests.append((method, path, json_body))
        return LIVE_PAYLOAD


class FakePool(BrowserPoolClient):
    def __init__(self) -> None:
        self.protocol = FakeProtocol()
        self.clients_for: list[tuple[str, str | None]] = []

    def _protocol_client(self, account_id: str, advertiser_id: str | None = None) -> FakeProtocol:
        self.clients_for.append((account_id, advertiser_id))
        return self.protocol


class FakeAccounts:
    path = ":memory:"

    def __init__(self, accounts: list[dict[str, Any]]) -> None:
        self.accounts = accounts

    def list_accounts(self) -> list[dict[str, Any]]:
        return self.accounts


async def test_fetch_asks_tiktok_through_an_account_with_a_session() -> None:
    pool = FakePool()
    accounts = FakeAccounts([
        {"id": "disabled", "enabled": False, "session_available": True},
        {"id": "no-session", "enabled": True, "session_available": False},
        {"id": "ready", "enabled": True, "session_available": True},
    ])
    service = QueueWaitService(get_client=lambda: pool, get_store=lambda: accounts)

    payload = await service._fetch(["2000004"])

    assert payload == LIVE_PAYLOAD
    assert pool.clients_for == [("ready", None)]
    assert pool.protocol.requests == [("POST", QUEUE_WAIT_PATH, {"strategy_ids": ["2000004"]})]
    with pytest.raises(QueueWaitUnavailable):
        await QueueWaitService(get_client=lambda: pool, get_store=lambda: FakeAccounts([]))._fetch(["1"])
    with pytest.raises(QueueWaitUnavailable):
        await QueueWaitService(get_client=lambda: object(), get_store=lambda: accounts)._fetch(["1"])


def make_service(tmp_path: Path, clock: Clock, *, client: Any = None) -> tuple[QueueWaitService, TaskStore, list[list[str]]]:
    task_store = TaskStore(str(tmp_path / "queue.db"))
    calls: list[list[str]] = []

    async def fetch(strategy_ids: list[str]) -> dict[str, Any]:
        calls.append(strategy_ids)
        return LIVE_PAYLOAD

    service = QueueWaitService(get_client=lambda: client, get_store=lambda: task_store, clock=clock)
    service.estimator = QueueWaitEstimator(fetch, clock=clock)
    return service, task_store, calls


async def test_schedule_submission_uses_a_fresh_estimate_without_waiting(tmp_path: Path) -> None:
    clock = Clock()
    service, task_store, calls = make_service(tmp_path, clock, client=FakePool())
    await service.estimator.estimates(["2000004"])

    service.schedule_submission(record("warm"), "reference")
    service.schedule_submission(record("unknown-mode"), "first_last")

    assert calls == [["2000004"]]
    assert service._log().get_many(["warm", "unknown-mode"]) == {
        "warm": {"task_id": "warm", "strategy_id": "2000004", "wait_seconds": 37398, "recorded_at": 1_000_000},
    }


async def test_schedule_submission_fetches_in_background_when_cold(tmp_path: Path) -> None:
    clock = Clock()
    service, _, calls = make_service(tmp_path, clock, client=FakePool())

    service.schedule_submission(record("cold"), "reference")
    assert service._log().get_many(["cold"]) == {}
    await asyncio.gather(*service._background)

    assert calls == [["2000004"]]
    assert service._log().get_many(["cold"])["cold"]["wait_seconds"] == 37398


def test_schedule_submission_outside_the_pool_records_only_the_strategy(tmp_path: Path) -> None:
    service, _, calls = make_service(tmp_path, Clock(), client=object())

    service.schedule_submission(record("plain"), "text")

    assert calls == []
    assert service._log().get_many(["plain"])["plain"]["strategy_id"] == "5000005"
    assert service._log().get_many(["plain"])["plain"]["wait_seconds"] is None


@pytest.fixture
def admin_api(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    import sd2api.main as main

    clock = Clock(2_000_000.0)
    service, task_store, calls = make_service(tmp_path, clock)
    monkeypatch.setattr(main, "store", task_store)
    monkeypatch.setattr(main, "queue_waits", service)
    monkeypatch.setattr(main.settings, "sd2api_admin_key", "admin-key-for-queue")
    return main, TestClient(main.app), task_store, service, clock, calls


def test_admin_tasks_attach_queue_details_to_pending_tasks(admin_api) -> None:
    main, api, task_store, service, clock, _ = admin_api
    task_store.create(task_id="waiting", api="seedance", model="seedance-2.0", prompt="p", seconds=15)
    task_store.update("waiting", status="running", progress=50)
    task_store.create(task_id="legacy", api="seedance", model="seedance-2.0", prompt="p", seconds=15)
    task_store.update("legacy", status="running", progress=50, raw={"data": {"draft_infos": [{"taskId": "legacy", "strategyId": "2000004"}]}})
    task_store.create(task_id="done", api="seedance", model="seedance-2.0", prompt="p", seconds=15)
    task_store.update("done", status="succeeded", progress=100)
    created = task_store.get("waiting").created_at
    clock.now = created + 600
    service._log().record("waiting", "2000004", 1_200, created)

    response = api.get("/admin/tasks", headers={"Authorization": "Bearer admin-key-for-queue"})

    assert response.status_code == 200
    items = {item["id"]: item for item in response.json()["data"]}
    assert items["waiting"]["queue"] == {
        "strategy_id": "2000004", "elapsed_seconds": 600, "current_wait_seconds": 37398,
        "checked_at": created + 600, "submit_wait_seconds": 1_200, "remaining_seconds": 600,
        "overdue_seconds": 0, "progress": 50,
    }
    assert items["legacy"]["queue"]["strategy_id"] == "2000004"
    assert items["legacy"]["queue"]["current_wait_seconds"] == 37398
    assert "submit_wait_seconds" not in items["legacy"]["queue"]
    assert "queue" not in items["done"]
    assert items["waiting"]["progress"] == 50  # the stored upstream progress is left alone


def test_admin_queue_wait_lists_estimates_per_model_and_mode(admin_api) -> None:
    _, api, _, _, _, calls = admin_api

    response = api.get("/admin/queue-wait", headers={"Authorization": "Bearer admin-key-for-queue"})

    assert response.status_code == 200
    assert response.json() == {
        "models": {
            "seedance-2.0": {"reference": {"strategy_id": "2000004", "seconds": 37398, "checked_at": 2_000_000}},
            "seedance-2.0-fast": {},
            "seedance-2.0-mini": {},
            "seedance-2.5": {},
        }
    }
    # Mini and Fast exist only for reference-to-video, so they add one strategy each.
    assert calls == [
        ["2000004", "2000008", "2000009", "2000012", "4000005", "4000008", "5000005", "5000007"]
    ]
    assert api.get("/admin/queue-wait").status_code == 401


def test_task_creation_records_the_submit_time_strategy(admin_api, monkeypatch: pytest.MonkeyPatch) -> None:
    main, api, _, service, _, _ = admin_api

    class FakeClient:
        async def create_text_video(self, **kwargs: object) -> str:
            return "created-1"

    monkeypatch.setattr(main, "client", FakeClient())
    response = api.post(
        "/v1/videos",
        headers={"Authorization": f"Bearer {main.settings.sd2api_api_key}"},
        json={"model": "seedance-2.0", "prompt": "A red ball", "seconds": 5},
    )

    assert response.status_code == 200
    assert "queue" not in response.json()
    assert service._log().get_many(["created-1"])["created-1"]["strategy_id"] == "5000005"
