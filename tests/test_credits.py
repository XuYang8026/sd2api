from __future__ import annotations

import datetime as dt

import pytest
from fastapi.testclient import TestClient

from sd2api.browser_pool import BrowserPoolClient
from sd2api.credits import next_weekly_reset


def utc(*args: int) -> int:
    return int(dt.datetime(*args, tzinfo=dt.timezone.utc).timestamp())


@pytest.mark.parametrize(
    ("now", "expected"),
    [
        (utc(2026, 11, 4, 12, 0), utc(2026, 11, 9)),
        (utc(2026, 11, 8, 23, 59, 59), utc(2026, 11, 9)),
        # Monday 04:00 in UTC+8 is still Sunday in UTC, so the reset is hours away.
        (utc(2026, 11, 8, 20, 0), utc(2026, 11, 9)),
        # Once Monday 00:00 UTC passes, this week's batch has started.
        (utc(2026, 11, 9), utc(2026, 11, 16)),
        (utc(2026, 11, 9, 1, 30), utc(2026, 11, 16)),
    ],
)
def test_next_weekly_reset_is_the_coming_monday_midnight_utc(now: int, expected: int) -> None:
    assert next_weekly_reset(now) == expected


def test_pool_status_reports_next_credit_reset(monkeypatch: pytest.MonkeyPatch) -> None:
    import sd2api.credits as credits
    import sd2api.main as main

    class FakePool(BrowserPoolClient):
        def __init__(self) -> None:
            pass

        async def status(self) -> dict[str, int]:
            return {"total": 1}

    monkeypatch.setattr(main, "client", FakePool())
    monkeypatch.setattr(main.settings, "sd2api_admin_key", "admin-key-for-credits")
    monkeypatch.setattr(credits.time, "time", lambda: utc(2026, 11, 4, 12, 0))

    response = TestClient(main.app).get(
        "/admin/pool/status", headers={"Authorization": "Bearer admin-key-for-credits"}
    )

    assert response.status_code == 200
    assert response.json() == {"total": 1, "credits_reset_at": utc(2026, 11, 9)}
