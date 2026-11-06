"""TikTok Creative Studio queue-wait estimates for the admin console.

TikTok's check endpoint reports ``draftTaskStatus``/``renderTaskStatus`` 2 both
while a task waits in TikTok's generation queue and while it renders, so a task
can look stuck at "running" for hours when the queue is long. Creative Studio
itself shows the wait from ``TrafficGateway/BatchQueryQueueWaitTime``: for each
strategy (one per model and workflow) it takes the longest of the returned
queues and refetches at most every 90 seconds. This module mirrors that, and
also remembers the estimate seen when each task was submitted so the console
can show how much of the expected wait is left.

Only the admin console uses this; the public task APIs are unchanged.
"""

from __future__ import annotations

import asyncio
import logging
import math
import sqlite3
import time
from collections.abc import Awaitable, Callable, Iterable
from typing import Any

from .browser_pool import BrowserPoolClient
from .protocol import I2V_MODELS, R2V_MODELS
from .store import TaskRecord, TaskStore
from .tiktok import T2V_MODELS

logger = logging.getLogger("sd2api.queue_wait")

QUEUE_WAIT_PATH = "/CreativeOne/TrafficGateway/BatchQueryQueueWaitTime"
# Creative Studio caches this query for 90 s (react-query staleTime 9e4).
CACHE_SECONDS = 90
# Don't show an estimate that could not be refreshed for this long.
MAX_ESTIMATE_AGE = 10 * 60
# Progress never reaches 100 from the estimate alone; only TikTok finishes a task.
MAX_ESTIMATED_PROGRESS = 95

MODE_MODELS: dict[str, dict[str, str]] = {
    "text": T2V_MODELS,
    "image": I2V_MODELS,
    "reference": R2V_MODELS,
}
# Model aliases offered by the admin generate form.
CONSOLE_MODELS = ("seedance-2.0", "seedance-2.0-fast", "seedance-2.0-mini", "seedance-2.5")
PENDING_STATUSES = {"queued", "running"}


class QueueWaitUnavailable(RuntimeError):
    """No logged-in pool account can ask TikTok for queue estimates."""


def strategy_for(mode: str, model: str) -> str | None:
    """Return TikTok's strategy ID for a generation mode and model alias."""
    return MODE_MODELS.get(mode, {}).get(model.strip().lower())


def parse_wait_estimates(payload: dict[str, Any]) -> dict[str, int]:
    """Map strategy ID to the longest estimated wait (seconds) among its queues."""
    estimates: dict[str, int] = {}
    for item in payload.get("results") or []:
        if not isinstance(item, dict):
            continue
        strategy_id = str(item.get("strategy_id") or "")
        waits = []
        for queue in item.get("queues") or []:
            try:
                value = float(queue.get("estimated_wait_seconds"))
            except (AttributeError, TypeError, ValueError):
                continue
            if math.isfinite(value) and value >= 0:
                waits.append(value)
        if strategy_id and waits:
            estimates[strategy_id] = int(max(waits))
    return estimates


class QueueWaitEstimator:
    """Cache TikTok's per-strategy wait estimates for ``CACHE_SECONDS``."""

    def __init__(
        self,
        fetch: Callable[[list[str]], Awaitable[dict[str, Any]]],
        *,
        ttl: float = CACHE_SECONDS,
        max_age: float = MAX_ESTIMATE_AGE,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._fetch = fetch
        self._ttl = ttl
        self._max_age = max_age
        self._clock = clock
        # strategy ID -> (seconds or None when TikTok returned no queue, checked_at)
        self._cache: dict[str, tuple[int | None, float]] = {}
        self._retry_at = 0.0
        self._lock = asyncio.Lock()

    def fresh(self, strategy_id: str) -> tuple[bool, int | None]:
        """``(True, seconds)`` when a cached answer is younger than the TTL, without fetching."""
        if strategy_id in self._cache and not self._stale([strategy_id], self._clock()):
            return True, self._cache[strategy_id][0]
        return False, None

    def _stale(self, strategy_ids: list[str], now: float) -> list[str]:
        return [
            strategy_id
            for strategy_id in strategy_ids
            if strategy_id not in self._cache
            or now - self._cache[strategy_id][1] >= self._ttl
        ]

    async def estimates(self, strategy_ids: Iterable[str | None]) -> dict[str, dict[str, int]]:
        wanted = sorted({strategy_id for strategy_id in strategy_ids if strategy_id})
        if wanted and self._stale(wanted, self._clock()):
            async with self._lock:
                now = self._clock()
                stale = self._stale(wanted, now)
                if stale and now >= self._retry_at:
                    try:
                        parsed = parse_wait_estimates(await self._fetch(stale))
                    except Exception as exc:  # noqa: BLE001 - estimates are best effort
                        # Keep serving what we have and back off instead of retrying on every poll.
                        self._retry_at = now + self._ttl
                        report = logger.debug if isinstance(exc, QueueWaitUnavailable) else logger.warning
                        report("Could not load TikTok queue estimates: %s", exc)
                    else:
                        for strategy_id in stale:
                            self._cache[strategy_id] = (parsed.get(strategy_id), now)
        now = self._clock()
        result: dict[str, dict[str, int]] = {}
        for strategy_id in wanted:
            seconds, checked_at = self._cache.get(strategy_id, (None, 0.0))
            if seconds is not None and now - checked_at <= self._max_age:
                result[strategy_id] = {"seconds": seconds, "checked_at": int(checked_at)}
        return result


class SubmissionLog:
    """Strategy and wait estimate seen when each task was submitted.

    Lives in its own table next to ``tasks`` so the upstream schema stays untouched.
    """

    def __init__(self, path: str) -> None:
        self.path = path
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS task_queue_estimates (
                    task_id TEXT PRIMARY KEY,
                    strategy_id TEXT NOT NULL,
                    wait_seconds INTEGER,
                    recorded_at INTEGER NOT NULL
                )
                """
            )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        return connection

    def record(
        self, task_id: str, strategy_id: str, wait_seconds: int | None, recorded_at: int
    ) -> None:
        with self._connect() as connection:
            connection.execute(
                "INSERT OR REPLACE INTO task_queue_estimates VALUES (?, ?, ?, ?)",
                (task_id, strategy_id, wait_seconds, recorded_at),
            )

    def get_many(self, task_ids: Iterable[str]) -> dict[str, dict[str, Any]]:
        ids = list(dict.fromkeys(task_ids))
        if not ids:
            return {}
        placeholders = ",".join("?" for _ in ids)
        with self._connect() as connection:
            rows = connection.execute(
                f"SELECT * FROM task_queue_estimates WHERE task_id IN ({placeholders})", ids
            ).fetchall()
        return {row["task_id"]: dict(row) for row in rows}


def raw_strategy(record: TaskRecord) -> str | None:
    """Strategy ID TikTok echoed in the task's draft (``strategyId`` / ``settings.aiModel``)."""
    data = (record.raw or {}).get("data")
    drafts = data.get("draft_infos") if isinstance(data, dict) else None
    if not isinstance(drafts, list):
        return None
    drafts = [draft for draft in drafts if isinstance(draft, dict)]
    draft = next((item for item in drafts if str(item.get("taskId")) == record.id), None)
    draft = draft or (drafts[0] if drafts else None)
    if draft is None:
        return None
    return str(draft.get("strategyId") or "") or None


def describe(
    record: TaskRecord,
    *,
    now: int,
    strategy_id: str | None,
    submitted_wait: int | None,
    current: dict[str, int] | None,
) -> dict[str, Any]:
    """Queue facts for a pending task, as shown in the admin console."""
    elapsed = max(0, now - int(record.created_at))
    info: dict[str, Any] = {"strategy_id": strategy_id, "elapsed_seconds": elapsed}
    if current:
        info["current_wait_seconds"] = current["seconds"]
        info["checked_at"] = current["checked_at"]
    if submitted_wait is not None:
        info["submit_wait_seconds"] = submitted_wait
        info["remaining_seconds"] = max(0, submitted_wait - elapsed)
        info["overdue_seconds"] = max(0, elapsed - submitted_wait)
        if submitted_wait > 0:
            info["progress"] = min(
                MAX_ESTIMATED_PROGRESS, max(1, round(elapsed * 100 / submitted_wait))
            )
    return info


class QueueWaitService:
    """Glue between the pool, the task store and the admin endpoints."""

    def __init__(
        self,
        *,
        get_client: Callable[[], Any],
        get_store: Callable[[], TaskStore],
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._get_client = get_client
        self._get_store = get_store
        self._clock = clock
        self.estimator = QueueWaitEstimator(self._fetch, clock=clock)
        self._logs: dict[str, SubmissionLog] = {}
        self._background: set[asyncio.Task[None]] = set()

    def _log(self) -> SubmissionLog:
        path = self._get_store().path
        if path not in self._logs:
            self._logs[path] = SubmissionLog(path)
        return self._logs[path]

    async def _fetch(self, strategy_ids: list[str]) -> dict[str, Any]:
        pool = self._get_client()
        if not isinstance(pool, BrowserPoolClient):
            raise QueueWaitUnavailable("queue estimates need SD2API_MODE=browser_pool")
        account = next(
            (
                item
                for item in self._get_store().list_accounts()
                if item.get("enabled") and item.get("session_available")
            ),
            None,
        )
        if account is None:
            raise QueueWaitUnavailable("no enabled account has a protocol session")
        # Reuse the pool's protocol client so session refreshes and shutdown close it too.
        protocol = pool._protocol_client(account["id"])
        return await protocol._request(
            "POST", QUEUE_WAIT_PATH, json_body={"strategy_ids": strategy_ids}
        )

    async def model_estimates(self) -> dict[str, Any]:
        """Current wait per console model and mode, for the generate form."""
        layout = {
            model: {mode: strategy_for(mode, model) for mode in MODE_MODELS}
            for model in CONSOLE_MODELS
        }
        known = await self.estimator.estimates(
            strategy_id for modes in layout.values() for strategy_id in modes.values()
        )
        return {
            "models": {
                model: {
                    mode: {"strategy_id": strategy_id, **known[strategy_id]}
                    for mode, strategy_id in modes.items()
                    if strategy_id in known
                }
                for model, modes in layout.items()
            }
        }

    def _save(self, task_id: str, strategy_id: str, wait_seconds: int | None) -> None:
        try:
            self._log().record(task_id, strategy_id, wait_seconds, int(self._clock()))
        except Exception:  # noqa: BLE001 - never let bookkeeping break submissions
            logger.exception("Could not record the queue estimate for task %s", task_id)

    async def record_submission(self, task_id: str, strategy_id: str) -> None:
        estimate = (await self.estimator.estimates([strategy_id])).get(strategy_id)
        self._save(task_id, strategy_id, estimate["seconds"] if estimate else None)

    def schedule_submission(self, record: TaskRecord, mode: str) -> None:
        """Remember the wait TikTok estimated when ``record`` was submitted.

        Uses the cached estimate when it is fresh (the generate page keeps it warm);
        otherwise fetches in the background so the API response never waits on TikTok.
        """
        strategy_id = strategy_for(mode, record.model)
        if strategy_id is None:
            return
        fresh, seconds = self.estimator.fresh(strategy_id)
        if fresh or not isinstance(self._get_client(), BrowserPoolClient):
            self._save(record.id, strategy_id, seconds)
            return
        task = asyncio.get_running_loop().create_task(
            self.record_submission(record.id, strategy_id)
        )
        self._background.add(task)
        task.add_done_callback(self._background.discard)

    async def annotate(self, records: list[TaskRecord], items: list[dict[str, Any]]) -> None:
        """Add a ``queue`` object to each pending task payload (``items`` matches ``records``)."""
        pending = [
            (record, item)
            for record, item in zip(records, items)
            if record.status in PENDING_STATUSES
        ]
        if not pending:
            return
        try:
            submitted = self._log().get_many(record.id for record, _ in pending)
        except sqlite3.Error:
            logger.exception("Could not read submit-time queue estimates")
            submitted = {}
        strategies = {
            record.id: (submitted.get(record.id) or {}).get("strategy_id") or raw_strategy(record)
            for record, _ in pending
        }
        current = await self.estimator.estimates(strategies.values())
        now = int(self._clock())
        for record, item in pending:
            strategy_id = strategies[record.id]
            item["queue"] = describe(
                record,
                now=now,
                strategy_id=strategy_id,
                submitted_wait=(submitted.get(record.id) or {}).get("wait_seconds"),
                current=current.get(strategy_id) if strategy_id else None,
            )
