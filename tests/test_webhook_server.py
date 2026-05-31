from __future__ import annotations

from unittest.mock import patch

import pytest

# The VM webhook stack (fastapi/uvicorn) is an optional, VM-only dependency.
# Skip cleanly where it isn't installed so the core suite stays green.
pytest.importorskip("fastapi")
from starlette.testclient import TestClient  # noqa: E402

from vm import webhook_server  # noqa: E402


PATH_SECRET = "pathsecret123"
TOKEN = "tokensecret456"


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("WEBHOOK_PATH_SECRET", PATH_SECRET)
    monkeypatch.setenv("WEBHOOK_SECRET_TOKEN", TOKEN)
    return TestClient(webhook_server.app)


def _skip_update():
    return {
        "update_id": 1,
        "callback_query": {"id": "c1", "data": "skip:4zida:abc",
                           "message": {"message_id": 9, "chat": {"id": 1}}},
    }


def test_health_ok(client):
    r = client.get("/health")
    assert r.status_code == 200 and r.json()["status"] == "ok"


def test_wrong_path_secret_404(client):
    r = client.post("/tg/WRONG", json=_skip_update(),
                    headers={"X-Telegram-Bot-Api-Secret-Token": TOKEN})
    assert r.status_code == 404


def test_wrong_header_token_403(client):
    r = client.post(f"/tg/{PATH_SECRET}", json=_skip_update(),
                    headers={"X-Telegram-Bot-Api-Secret-Token": "nope"})
    assert r.status_code == 403


def test_valid_skip_dispatches_and_persists(client):
    with patch("vm.webhook_server._apply_callback") as apply_mock:
        r = client.post(f"/tg/{PATH_SECRET}", json=_skip_update(),
                        headers={"X-Telegram-Bot-Api-Secret-Token": TOKEN})
    assert r.status_code == 200 and r.json() == {"ok": True}
    apply_mock.assert_called_once()
    # The callback_query payload is forwarded verbatim to the applier.
    assert apply_mock.call_args.args[0]["data"] == "skip:4zida:abc"


def test_non_callback_update_is_accepted_and_ignored(client):
    with patch("vm.webhook_server._apply_callback") as apply_mock:
        r = client.post(f"/tg/{PATH_SECRET}", json={"update_id": 2, "message": {"text": "hi"}},
                        headers={"X-Telegram-Bot-Api-Secret-Token": TOKEN})
    assert r.status_code == 200
    apply_mock.assert_not_called()


def test_apply_failure_still_returns_200(client):
    # An internal error must not make Telegram retry-storm the endpoint.
    with patch("vm.webhook_server._apply_callback", side_effect=RuntimeError("boom")):
        r = client.post(f"/tg/{PATH_SECRET}", json=_skip_update(),
                        headers={"X-Telegram-Bot-Api-Secret-Token": TOKEN})
    assert r.status_code == 200 and r.json() == {"ok": True}


def test_apply_callback_pulls_applies_pushes(monkeypatch):
    # _apply_callback orchestrates pull → handle → push under the lock.
    calls = []
    monkeypatch.setattr(webhook_server.state, "pull", lambda: calls.append("pull"))
    monkeypatch.setattr(webhook_server.state, "push", lambda: calls.append("push"))

    class FakeConn:
        def close(self):
            calls.append("close")
    monkeypatch.setattr(webhook_server.state, "ensure_schema", lambda: FakeConn())

    def fake_handle(conn, cq, counts):
        calls.append("handle")
        counts["skipped"] += 1
    monkeypatch.setattr(webhook_server.telegram_callbacks, "handle_callback_query", fake_handle)

    counts = webhook_server._apply_callback({"id": "c1", "data": "skip:x:y"})
    assert counts["skipped"] == 1
    # Order matters: pull before handle before push; conn closed before push.
    assert calls == ["pull", "handle", "close", "push"]
