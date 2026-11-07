"""Creative Studio credits: the weekly refresh schedule and per-job cost.

TikTok refreshes every account's credits in a batch that starts on Monday at
00:00 UTC; the credit ledger (``QueryCreditLedgerEntries``, action
``WEEKLY_REFRESH``) shows the grant landing between 00:19 and 02:20 UTC, and
unused credits do not roll over. The studio's own credit page also labels the
next refresh as the coming Monday rather than a per-account date.
"""

from __future__ import annotations

import datetime as dt
import time

from .tiktok import FAST_ALIASES, MINI_ALIASES, T2V_MODELS

RESET_WEEKDAY = 0  # Monday

# ``cost.creditsPerSecond`` from the studio's model registry (web bundle of
# 2026-11-05). T2V, I2V and R2V charge the same rate for one model family.
CREDITS_PER_SECOND: dict[str, int] = {
    **{alias: 10 if internal == "5000007" else 5 for alias, internal in T2V_MODELS.items()},
    **{alias: 3 for alias in FAST_ALIASES},
    **{alias: 2 for alias in MINI_ALIASES},
}


def next_weekly_reset(now: float | None = None) -> int:
    """Return the Unix time of the next Monday 00:00 UTC strictly after ``now``."""
    current = dt.datetime.fromtimestamp(
        time.time() if now is None else now, dt.timezone.utc
    )
    midnight = current.replace(hour=0, minute=0, second=0, microsecond=0)
    days = (RESET_WEEKDAY - midnight.weekday()) % 7 or 7
    return int((midnight + dt.timedelta(days=days)).timestamp())


def estimated_job_credits(model: str, duration: int) -> int | None:
    """Credits TikTok charges for one job, or ``None`` for an unpriced model."""
    rate = CREDITS_PER_SECOND.get(model.lower())
    return None if rate is None else rate * duration
