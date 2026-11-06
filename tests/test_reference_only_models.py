"""Seedance 2.0 Mini and Fast: TikTok offers them for reference-to-video only."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from sd2api.browser_client import BrowserTikTokClient, browser_model_label, shows_model
from sd2api.config import Settings
from sd2api.main import upstream_model_name
from sd2api.protocol import ProtocolTikTokClient
from sd2api.queue_wait import CONSOLE_MODELS, strategy_for
from sd2api.tiktok import TikTokUpstreamError
from sd2api.uploads import StagedMedia
from test_tiktok import png_bytes, protocol_session


def recording_client(requests: list[dict[str, Any]]) -> ProtocolTikTokClient:
    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        return httpx.Response(200, json={"code": 0, "data": {"taskId": "task"}})

    client = ProtocolTikTokClient(
        Settings(),
        protocol_session(),
        account_id="account-a",
        advertiser_id="123456789012",
        transport=httpx.MockTransport(handler),
    )

    async def fake_upload(media: StagedMedia):
        return type(
            "Media",
            (),
            {"kind": media.kind, "image_url": "https://img/reference", "vid": None, "poster_url": None},
        )()

    client.upload_media = fake_upload  # type: ignore[method-assign]
    return client


def reference_image(tmp_path: Path) -> Path:
    image = tmp_path / "reference.png"
    image.write_bytes(png_bytes())
    return image


@pytest.mark.parametrize(
    ("model", "strategy"),
    [
        ("seedance-2.0-mini", "2000009"),
        ("Dreamina-Seedance-2.0-Mini", "2000009"),
        ("seedance2_mini", "2000009"),
        ("seedance-2.0-fast", "2000012"),
        ("seedance2.0-fast", "2000012"),
        ("dreamina-seedance-2-0-fast", "2000012"),
    ],
)
async def test_reference_video_submits_mini_and_fast_strategies(
    tmp_path: Path, model: str, strategy: str
) -> None:
    requests: list[dict[str, Any]] = []
    client = recording_client(requests)

    await client.create_reference_video(
        prompt="reference",
        model=model,
        duration=10,
        media=[StagedMedia(kind="image", path=str(reference_image(tmp_path)))],
    )
    await client.close()

    assert requests[0]["model"] == strategy
    assert json.loads(requests[0]["settings"])["aiModel"] == strategy
    assert requests[0]["duration"] == 10


@pytest.mark.parametrize("model", ["seedance-2.0-mini", "seedance-2.0-fast"])
async def test_mini_and_fast_reject_text_and_image_modes_before_calling_tiktok(
    tmp_path: Path, model: str
) -> None:
    requests: list[dict[str, Any]] = []
    client = recording_client(requests)

    with pytest.raises(TikTokUpstreamError) as text_error:
        await client.create_text_video(prompt="text", model=model, duration=5)
    with pytest.raises(TikTokUpstreamError) as image_error:
        await client.create_image_video(
            prompt="image", model=model, duration=5, image_path=str(reference_image(tmp_path))
        )
    await client.close()

    for error in (text_error.value, image_error.value):
        assert error.status_code == 400
        assert error.code == "model_mode_unsupported"
        assert "reference" in str(error)
    assert requests == []


async def test_browser_mode_rejects_fast_for_text_before_opening_a_browser(tmp_path: Path) -> None:
    client = BrowserTikTokClient(
        Settings(sd2api_browser_profile=str(tmp_path / "profile")),
        account_id="browser-a",
    )

    with pytest.raises(TikTokUpstreamError) as error:
        await client.create_text_video(prompt="text", model="seedance-2.0-fast", duration=5)

    assert error.value.status_code == 400
    assert error.value.code == "model_mode_unsupported"


@pytest.mark.parametrize(
    ("model", "label"),
    [
        ("seedance-2.0", "Dreamina Seedance 2.0"),
        ("sora-2", "Dreamina Seedance 2.0"),
        ("seedance-2.5", "Dreamina Seedance 2.5"),
        ("seedance-2.0-mini", "Dreamina Seedance 2.0 Mini"),
        ("Seedance2_Fast", "Dreamina Seedance 2.0 Fast"),
    ],
)
def test_browser_model_label_matches_the_creative_studio_menu(model: str, label: str) -> None:
    assert browser_model_label(model) == label


def test_browser_selector_does_not_mistake_fast_for_plain_2_0() -> None:
    assert shows_model("Dreamina Seedance 2.0", "Dreamina Seedance 2.0")
    assert shows_model("Dreamina Seedance 2.0 Fast\n", "Dreamina Seedance 2.0 Fast")
    assert not shows_model("Dreamina Seedance 2.0 Fast", "Dreamina Seedance 2.0")
    assert not shows_model("Dreamina Seedance 2.0 Mini", "Dreamina Seedance 2.0")


@pytest.mark.parametrize(
    ("raw", "canonical"),
    [
        ("Seedance-2.0-Fast", "seedance-2.0-fast"),
        ("seedance2_fast", "seedance-2.0-fast"),
        ("dreamina-seedance-2-0-mini", "seedance-2.0-mini"),
        ("seedance-2.0", "seedance-2.0"),
    ],
)
def test_upstream_model_name_canonicalizes_mini_and_fast(raw: str, canonical: str) -> None:
    assert upstream_model_name(raw) == canonical


@pytest.mark.parametrize(
    ("mode", "model", "expected"),
    [
        ("reference", "seedance-2.0-mini", "2000009"),
        ("reference", "seedance-2.0-fast", "2000012"),
        ("text", "seedance-2.0-fast", None),
        ("image", "seedance-2.0-mini", None),
    ],
)
def test_queue_wait_knows_mini_and_fast_reference_strategies(
    mode: str, model: str, expected: str | None
) -> None:
    assert strategy_for(mode, model) == expected
    assert {"seedance-2.0-mini", "seedance-2.0-fast"} <= set(CONSOLE_MODELS)


def test_admin_console_offers_mini_and_fast(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from sd2api import main

    api = TestClient(main.app)
    page = api.get("/admin").text
    script = api.get("/admin/assets/admin.js").text

    assert '<option value="seedance-2.0-fast">' in page
    assert '<option value="seedance-2.0-mini">' in page
    # Mini/Fast are R2V-only, and the estimate must follow TikTok's per-model price.
    assert "REFERENCE_ONLY_MODELS" in script
    assert "CREDITS_PER_SECOND" in script
