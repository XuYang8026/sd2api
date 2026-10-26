from __future__ import annotations

import asyncio
import base64
from collections.abc import Awaitable, Callable
import hashlib
import hmac
from pathlib import Path
import time
from urllib.parse import quote
import uuid


SHARE_LINK_CONTEXT = b"sd2api-share-link-v1"
CACHE_MAX_AGE_SECONDS = 24 * 60 * 60
CACHE_MAX_BYTES = 2 * 1024 * 1024 * 1024


def share_signature(secret: str, task_id: str) -> str:
    """Return a URL-safe 128-bit HMAC that lets anyone holding it fetch one video."""
    key = hmac.new(secret.encode("utf-8"), SHARE_LINK_CONTEXT, hashlib.sha256).digest()
    digest = hmac.new(key, task_id.encode("utf-8"), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(digest[:16]).rstrip(b"=").decode("ascii")


def verify_share_signature(secret: str, task_id: str, signature: str) -> bool:
    return hmac.compare_digest(share_signature(secret, task_id), signature)


def share_path(secret: str, task_id: str) -> str:
    return f"/share/videos/{quote(task_id, safe='')}.mp4?sig={share_signature(secret, task_id)}"


class VideoCache:
    """Keeps recently shared videos on disk so range requests do not refetch from TikTok."""

    def __init__(
        self,
        directory: Path,
        *,
        max_age_seconds: int = CACHE_MAX_AGE_SECONDS,
        max_bytes: int = CACHE_MAX_BYTES,
    ) -> None:
        self.directory = directory
        self.max_age_seconds = max_age_seconds
        self.max_bytes = max_bytes
        self._locks: dict[str, asyncio.Lock] = {}

    def path_for(self, task_id: str) -> Path:
        return self.directory / f"{hashlib.sha256(task_id.encode('utf-8')).hexdigest()}.mp4"

    async def get(self, task_id: str, fetch: Callable[[], Awaitable[bytes]]) -> Path:
        path = self.path_for(task_id)
        if self._is_fresh(path):
            return path
        async with self._locks.setdefault(task_id, asyncio.Lock()):
            if self._is_fresh(path):
                return path
            body = await fetch()
            await asyncio.to_thread(self._write, path, body)
            await asyncio.to_thread(self.prune, keep=path)
            return path

    def prune(self, *, keep: Path | None = None) -> None:
        if not self.directory.is_dir():
            return
        cutoff = time.time() - self.max_age_seconds
        entries = []
        for path in self.directory.iterdir():
            try:
                stat = path.stat()
            except FileNotFoundError:
                continue
            if path != keep and stat.st_mtime < cutoff:
                path.unlink(missing_ok=True)
            elif path.suffix == ".mp4":
                entries.append((stat.st_mtime, stat.st_size, path))
        total = sum(size for _, size, _ in entries)
        for _, size, path in sorted(entries):
            if total <= self.max_bytes:
                break
            if path != keep:
                path.unlink(missing_ok=True)
                total -= size

    def _is_fresh(self, path: Path) -> bool:
        try:
            return time.time() - path.stat().st_mtime < self.max_age_seconds
        except FileNotFoundError:
            return False

    @staticmethod
    def _write(path: Path, body: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f"{path.name}.{uuid.uuid4().hex}.tmp")
        try:
            temporary.write_bytes(body)
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)
