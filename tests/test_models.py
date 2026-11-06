from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from sd2api.store import TaskStore


@pytest.fixture
def models_api(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from sd2api import main

    class NoUpstream:
        def __getattr__(self, name):
            raise AssertionError(f"Model discovery must not access upstream: {name}")

    monkeypatch.setattr(main, "store", TaskStore(str(tmp_path / "models.db")))
    monkeypatch.setattr(main, "client", NoUpstream())
    monkeypatch.setattr(main.settings, "sd2api_api_key", "test-api-key")
    monkeypatch.setattr(main.settings, "sd2api_admin_key", "test-admin-key")
    # Do not enter TestClient's lifespan: discovery must work without browser login.
    return main, TestClient(main.app)


def test_models_returns_stable_openai_catalog_without_upstream(models_api):
    _, api = models_api
    headers = {"Authorization": "Bearer test-api-key"}
    response = api.get("/v1/models", headers=headers)
    assert response.status_code == 200
    expected_ids = [
        "seedance-2.0", "seedance-2.0-fast", "seedance-2.0-mini",
        "seedance-2.5", "sora-2",
    ]
    assert response.json() == {
        "object": "list",
        "data": [
            {"id": model, "object": "model", "created": 0, "owned_by": "sd2api"}
            for model in expected_ids
        ],
    }
    assert api.get("/v1/models", headers=headers).json() == response.json()


@pytest.mark.parametrize("authorization", [None, "Bearer wrong-key", "Basic test-api-key"])
def test_models_rejects_missing_or_invalid_keys(models_api, authorization):
    _, api = models_api
    headers = {"Authorization": authorization} if authorization else {}
    assert api.get("/v1/models", headers=headers).status_code == 401


def test_models_accepts_existing_admin_key(models_api):
    _, api = models_api
    assert api.get("/v1/models", headers={"Authorization": "Bearer test-admin-key"}).status_code == 200


def test_models_accepts_managed_key_and_rejects_revoked_key(models_api):
    main, api = models_api
    main.store.create_api_key(key_id="managed", name="Test", api_key="managed-key", key_mask="ma***ey")
    headers = {"Authorization": "Bearer managed-key"}
    assert api.get("/v1/models", headers=headers).status_code == 200
    main.store.delete_api_key("managed")
    assert api.get("/v1/models", headers=headers).status_code == 401


def test_models_preserves_unconfigured_auth_mode(models_api, monkeypatch):
    main, api = models_api
    monkeypatch.setattr(main.settings, "sd2api_api_key", "")
    monkeypatch.setattr(main.settings, "sd2api_admin_key", "")
    assert api.get("/v1/models").status_code == 200


def test_managed_key_alone_still_requires_auth(models_api, monkeypatch):
    main, api = models_api
    monkeypatch.setattr(main.settings, "sd2api_api_key", "")
    monkeypatch.setattr(main.settings, "sd2api_admin_key", "")
    main.store.create_api_key(key_id="managed", name="Test", api_key="managed-key", key_mask="ma***ey")
    assert api.get("/v1/models").status_code == 401
    assert api.get("/v1/models", headers={"Authorization": "Bearer managed-key"}).status_code == 200
