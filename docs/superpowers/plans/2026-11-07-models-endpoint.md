# Models Endpoint Implementation Plan

> **For agentic workers:** Use superpowers:executing-plans to implement this approved plan inline, with test-driven-development and verification-before-completion.

**Goal:** Add authenticated, OpenAI-compatible model discovery and deploy it to the existing USA VPS so ArcReel's connection test and model discovery work.

**Architecture:** `GET /v1/models` uses the existing `require_api_key` dependency. It returns a deterministic adapter catalog, not an account entitlement check, and never calls TikTok. Existing generation routes and their capability restrictions remain unchanged.

**Tech Stack:** FastAPI, pytest, httpx/OpenAI SDK for integration verification, Docker Compose.

## Approved contract

Return `{"object":"list","data":[...]}`. Each item has `id`, `object: "model"`, `created: 0` (unknown creation date), and `owned_by: "sd2api"`. IDs in order: `seedance-2.0`, `seedance-2.0-fast`, `seedance-2.0-mini`, `seedance-2.5`, `sora-2`. The last is only an alias, not Sora. Fast/Mini remain reference-only; 2.5 still requires upstream access. Reuse all existing auth modes, including managed keys and explicitly unconfigured auth.

## Files

- Add `tests/test_models.py`: response contract, stable order/metadata, missing/wrong/static/admin/managed/revoked keys, no upstream client access.
- Modify `sd2api/main.py`: one protected GET route adjacent to the health route.
- Modify `README.md`: endpoint, curl example, catalog-vs-permission distinction, ArcReel caveats.

## Execution

- [ ] Run baseline: `SD2API_API_KEY=unit-test-api-key PYTHONPATH=. /Users/xuyang/workspace/sd2api/.venv/bin/python -m pytest -q`.
- [ ] Write tests first using a temporary TaskStore and fixed test-only credentials; replace the upstream client with an object that raises on access.
- [ ] Run `pytest tests/test_models.py -q`; expect endpoint assertions to fail with HTTP 404.
- [ ] Add the route using the existing FAST_MODEL and MINI_MODEL constants; return a fresh JSON list on each request.
- [ ] Run focused and full tests with the baseline command; run `git diff --check`.
- [ ] Review only the scoped diff, preserve unrelated files, and record the feature in Git.
- [ ] Inspect VPS working tree, active tasks, and current image; retain the old image and deployment configuration for rollback.
- [ ] Transfer only the feature changes, build a tagged image, and recreate only sd2api when no video tasks are running. Do not modify secrets or data volumes.
- [ ] Verify health, unauthenticated/invalid-key 401, authenticated model catalog 200, then ArcReel's saved-provider test and discovery APIs. Never submit a paid generation for this change.
- [ ] Sync verified source to the original local checkout, update local usage notes, and report the final test/deployment results.

## Rollback

Restore the prior image reference and recreate only the sd2api service using the unchanged data volumes. Restore tracked source with a scoped reverse feature patch if necessary; do not reset or delete user data.

## Execution results — 2026-11-07

- Feature commit: `c29310e` on local `custom` and USA VPS `/opt/sd2api`; no GitHub push.
- Clean baseline: 141 passed after setting a dummy `SD2API_API_KEY` required by an existing queue test.
- Red phase: all 8 new tests failed with the expected 404 responses.
- Green phase: 8 focused tests and 149 total tests passed. Full tests also passed inside the new Linux deployment image and after syncing the original local checkout. Existing dependency deprecation warnings remain.
- Only source, tests and documentation changed. Production has no queued/running tasks at replacement time, and all existing data volumes and credentials were retained.
- Deployed image: `sd2api-fork:models-20261107`, also tagged `sd2api-fork:latest` to preserve existing Compose configuration. Rollback image: `sd2api-fork:rollback-before-models-20261107`.
- ArcReel saved-provider test returned HTTP 200, `success=true`, `model_count=5`; discovery returned all five models.
- From the ArcReel container: missing/invalid credentials returned 401; saved credentials returned 200 with five entries. No generation request was made.
- Discovery does not fix Fast/Mini input adaptation or the existing Ark SDK nullable-parameter mismatch. ArcReel heuristically selects `ark-seedance` for newly discovered Seedance IDs; keep the already configured ordinary model on `openai-video` until the separate compatibility work is done. The `sora-2` alias must not be interpreted or priced as genuine Sora.
