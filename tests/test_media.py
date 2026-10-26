from __future__ import annotations

import asyncio
import os
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from sd2api.media import VideoCache, share_path, share_signature, verify_share_signature
from sd2api.store import TaskStore


VIDEO = b"\x00\x00\x00\x18ftypisom" + bytes(range(256)) * 4


def test_share_signature_is_stable_and_bound_to_secret_and_task() -> None:
    signature = share_signature("secret", "7701012386938421249")

    assert signature == share_signature("secret", "7701012386938421249")
    assert signature != share_signature("other-secret", "7701012386938421249")
    assert signature != share_signature("secret", "7701012386938421250")
    assert len(signature) == 22
    assert set(signature) <= set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_")


def test_verify_share_signature_rejects_tampered_values() -> None:
    signature = share_signature("secret", "task-1")

    assert verify_share_signature("secret", "task-1", signature)
    assert not verify_share_signature("secret", "task-2", signature)
    assert not verify_share_signature("secret", "task-1", signature[:-1] + "A" if signature[-1] != "A" else "B")
    assert not verify_share_signature("secret", "task-1", "")


def test_share_path_points_at_signed_mp4_route() -> None:
    assert share_path("secret", "task-1") == (
        f"/share/videos/task-1.mp4?sig={share_signature('secret', 'task-1')}"
    )


async def test_video_cache_fetches_once_for_concurrent_requests(tmp_path: Path) -> None:
    cache = VideoCache(tmp_path / "cache")
    calls = 0

    async def fetch() -> bytes:
        nonlocal calls
        calls += 1
        await asyncio.sleep(0.01)
        return VIDEO

    paths = await asyncio.gather(*(cache.get("task-1", fetch) for _ in range(5)))

    assert calls == 1
    assert len(set(paths)) == 1
    assert paths[0].read_bytes() == VIDEO


async def test_video_cache_refetches_expired_entries(tmp_path: Path) -> None:
    cache = VideoCache(tmp_path / "cache", max_age_seconds=60)
    bodies = iter([b"old", b"new"])

    async def fetch() -> bytes:
        return next(bodies)

    path = await cache.get("task-1", fetch)
    expired = time.time() - 120
    os.utime(path, (expired, expired))

    assert (await cache.get("task-1", fetch)).read_bytes() == b"new"


async def test_video_cache_leaves_no_file_when_fetch_fails(tmp_path: Path) -> None:
    cache = VideoCache(tmp_path / "cache")

    async def fetch() -> bytes:
        raise RuntimeError("upstream failed")

    with pytest.raises(RuntimeError):
        await cache.get("task-1", fetch)

    assert list((tmp_path / "cache").glob("*")) == []


async def test_video_cache_evicts_oldest_entries_over_size_limit(tmp_path: Path) -> None:
    cache = VideoCache(tmp_path / "cache", max_bytes=25)

    async def fetch() -> bytes:
        return b"x" * 10

    first = await cache.get("task-1", fetch)
    older = time.time() - 30
    os.utime(first, (older, older))
    second = await cache.get("task-2", fetch)
    third = await cache.get("task-3", fetch)

    assert not first.exists()
    assert second.exists()
    assert third.exists()


def test_video_cache_file_names_do_not_follow_task_ids(tmp_path: Path) -> None:
    cache = VideoCache(tmp_path / "cache")

    path = cache.path_for("../../escape")

    assert path.parent == tmp_path / "cache"
    assert path.suffix == ".mp4"


@pytest.fixture
def shared_api(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    import sd2api.main as main

    class FakeBrowserClient:
        fetches: list[str] = []

        async def fetch_video(self, video_url: str) -> tuple[bytes, str]:
            self.fetches.append(video_url)
            return VIDEO, "video/mp4"

    fake = FakeBrowserClient()
    task_store = TaskStore(str(tmp_path / "media.db"))
    monkeypatch.setattr(main, "BrowserTikTokClient", FakeBrowserClient)
    monkeypatch.setattr(main, "client", fake)
    monkeypatch.setattr(main, "store", task_store)
    monkeypatch.setattr(main, "video_cache", VideoCache(tmp_path / "video-cache"))
    monkeypatch.setattr(main.settings, "sd2api_admin_key", "admin-key-for-media")
    monkeypatch.setattr(main.settings, "sd2api_credential_key", "credential-key-for-media")

    task_store.create(task_id="done-1", api="openai", model="sora-2", prompt="A red ball", seconds=5)
    task_store.update(
        "done-1",
        status="succeeded",
        progress=100,
        video_url="https://v16-ad-creative.tiktokcdn-row.com/video/done-1",
        poster_url="https://p16-common-sign.tiktokcdn.com/poster/done-1.jpeg",
    )
    task_store.create(task_id="running-1", api="openai", model="sora-2", prompt="A blue ball", seconds=5)
    task_store.update("running-1", status="running", progress=40)
    return main, TestClient(main.app), fake


def test_shared_video_downloads_without_api_key(shared_api) -> None:
    main, api, fake = shared_api

    response = api.get(share_path(main.settings.credential_master_key, "done-1"))

    assert response.status_code == 200
    assert response.content == VIDEO
    assert response.headers["content-type"] == "video/mp4"
    assert response.headers["content-disposition"] == 'attachment; filename="done-1.mp4"'
    assert response.headers["accept-ranges"] == "bytes"
    assert fake.fetches == ["https://v16-ad-creative.tiktokcdn-row.com/video/done-1"]


def test_shared_video_serves_ranges_from_cache(shared_api) -> None:
    main, api, fake = shared_api
    url = share_path(main.settings.credential_master_key, "done-1")

    api.get(url)
    response = api.get(url, headers={"Range": "bytes=10-19"})

    assert response.status_code == 206
    assert response.content == VIDEO[10:20]
    assert response.headers["content-range"] == f"bytes 10-19/{len(VIDEO)}"
    assert len(fake.fetches) == 1


def test_shared_video_head_returns_headers(shared_api) -> None:
    main, api, _ = shared_api

    response = api.head(share_path(main.settings.credential_master_key, "done-1"))

    assert response.status_code == 200
    assert response.headers["content-length"] == str(len(VIDEO))


@pytest.mark.parametrize(
    ("task_id", "signature"),
    [
        ("done-1", "wrong-signature-value1"),
        ("done-1", ""),
        ("missing-1", None),
    ],
)
def test_shared_video_hides_bad_signatures_and_unknown_tasks(shared_api, task_id, signature) -> None:
    main, api, fake = shared_api
    signature = share_signature(main.settings.credential_master_key, task_id) if signature is None else signature

    response = api.get(f"/share/videos/{task_id}.mp4", params={"sig": signature})

    assert response.status_code == 404
    assert fake.fetches == []


def test_shared_video_rejects_unfinished_tasks(shared_api) -> None:
    main, api, fake = shared_api

    response = api.get(share_path(main.settings.credential_master_key, "running-1"))

    assert response.status_code == 409
    assert fake.fetches == []


def test_shared_video_stops_working_after_record_is_deleted(shared_api) -> None:
    main, api, _ = shared_api
    url = share_path(main.settings.credential_master_key, "done-1")
    assert api.get(url).status_code == 200

    main.store.delete("done-1")

    assert api.get(url).status_code == 404


def test_admin_tasks_include_poster_and_share_links(shared_api) -> None:
    main, api, _ = shared_api

    response = api.get("/admin/tasks", headers={"Authorization": "Bearer admin-key-for-media"})

    tasks = {task["id"]: task for task in response.json()["data"]}
    assert tasks["done-1"]["poster_url"] == "https://p16-common-sign.tiktokcdn.com/poster/done-1.jpeg"
    assert tasks["done-1"]["share_url"] == share_path(main.settings.credential_master_key, "done-1")
    assert tasks["running-1"]["poster_url"] is None
    assert tasks["running-1"]["share_url"] is None


def test_share_links_never_use_an_empty_secret(shared_api, monkeypatch: pytest.MonkeyPatch) -> None:
    main, api, _ = shared_api
    monkeypatch.setattr(main.settings, "sd2api_credential_key", "")
    monkeypatch.setattr(main.settings, "sd2api_admin_key", "")
    monkeypatch.setattr(main.settings, "sd2api_api_key", "")

    tasks = {task["id"]: task for task in api.get("/admin/tasks").json()["data"]}
    share_url = tasks["done-1"]["share_url"]

    assert share_url != share_path("", "done-1")
    assert api.get(share_url).status_code == 200
    assert api.get(share_path("", "done-1")).status_code == 404


@pytest.mark.parametrize(
    ("path", "content_type"),
    [
        ("/admin/assets/admin-media.js", "application/javascript"),
        ("/admin/assets/admin-media.css", "text/css"),
    ],
)
def test_admin_media_assets_are_served(shared_api, path: str, content_type: str) -> None:
    _, api, _ = shared_api

    response = api.get(path)

    assert response.status_code == 200
    assert response.headers["content-type"].startswith(content_type)


def test_admin_page_loads_media_assets(shared_api) -> None:
    _, api, _ = shared_api

    page = api.get("/admin").text

    assert "/admin/assets/admin-media.css" in page
    assert "/admin/assets/admin-media.js" in page
