"""Weekly Creative Studio credit schedule.

TikTok refreshes every account's credits in a batch that starts on Monday at
00:00 UTC; the credit ledger (``QueryCreditLedgerEntries``, action
``WEEKLY_REFRESH``) shows the grant landing between 00:19 and 02:20 UTC, and
unused credits do not roll over. The studio's own credit page also labels the
next refresh as the coming Monday rather than a per-account date.
"""

from __future__ import annotations

import datetime as dt
import time

RESET_WEEKDAY = 0  # Monday


def next_weekly_reset(now: float | None = None) -> int:
    """Return the Unix time of the next Monday 00:00 UTC strictly after ``now``."""
    current = dt.datetime.fromtimestamp(
        time.time() if now is None else now, dt.timezone.utc
    )
    midnight = current.replace(hour=0, minute=0, second=0, microsecond=0)
    days = (RESET_WEEKDAY - midnight.weekday()) % 7 or 7
    return int((midnight + dt.timedelta(days=days)).timestamp())
