from __future__ import annotations

import base64
import os
import signal
import time
from typing import Any

import httpx
import pytest

from app.constants import PROBLEM_JSON
from app.main import create_app
from app.settings import Settings
from tests.conftest import NO_SANDBOX, auth_headers
from tests.fake_keyring import FakeKeyring


async def create_env(
    client: httpx.AsyncClient, headers: dict[str, str], **body: Any
) -> dict[str, Any]:
    payload = {"name": "env", **body}
    response = await client.post("/v1/environments", json=payload, headers=headers)
    assert response.status_code == 201, response.text
    data: dict[str, Any] = response.json()
    return data


async def open_shell(
    client: httpx.AsyncClient, headers: dict[str, str], env_id: str, **body: Any
) -> dict[str, Any]:
    response = await client.post(f"/v1/environments/{env_id}/shells", json=body, headers=headers)
    assert response.status_code == 201, response.text
    data: dict[str, Any] = response.json()
    return data


def problem(response: httpx.Response) -> dict[str, Any]:
    assert response.headers["content-type"].startswith(PROBLEM_JSON)
    body: dict[str, Any] = response.json()
    assert body["status"] == response.status_code and "code" in body and "instance" in body
    return body


async def test_health(client: httpx.AsyncClient, keyring: FakeKeyring) -> None:
    response = await client.get("/health")
    assert response.status_code == 200 and response.json()["status"] == "ok"
    assert "x-request-id" in response.headers
    response = await client.get("/health/ready")
    assert response.status_code == 200
    body = response.json()
    assert body["sandbox_tier"] == "directory" and body["keyring"]["status"] == "fresh"
    keyring.down = True
    response = await client.get("/health/ready")
    assert response.status_code == 200
    assert response.json()["keyring"] == {"status": "cached", "keys_cached": True, "error": None}


async def test_ready_without_keys_is_503(settings: Settings, keyring: FakeKeyring) -> None:
    keyring.down = True
    app = create_app(settings, http_client=keyring.client(), capabilities=NO_SANDBOX)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
            response = await client.get("/health/ready")
            assert response.status_code == 503
            assert response.json()["keyring"]["status"] == "unreachable"
            assert response.json()["keyring"]["error"]
            response = await client.get("/v1/environments", headers=auth_headers(keyring, "a"))
            assert (
                response.status_code == 503 and problem(response)["code"] == "keyring_unavailable"
            )


async def test_auth_failures(client: httpx.AsyncClient, keyring: FakeKeyring) -> None:
    response = await client.get("/v1/environments")
    assert response.status_code == 401 and problem(response)["code"] == "unauthorized"
    bad = {"X-Keyring-User-Token": keyring.mint("a", audience="web-search-api")}
    response = await client.get("/v1/environments", headers=bad)
    assert problem(response)["token_error"] == "wrong_audience"
    response = await client.get(
        "/v1/environments",
        headers={**auth_headers(keyring, "a"), "X-Keyring-Profile": "Bad Profile"},
    )
    assert response.status_code == 422 and problem(response)["code"] == "validation_error"
    response = await client.post(
        "/v1/environments", json={"name": ""}, headers=auth_headers(keyring, "a")
    )
    assert response.status_code == 422 and problem(response)["errors"]


async def test_api_key_gate(settings: Settings, keyring: FakeKeyring) -> None:
    settings = settings.model_copy(update={"api_keys": ["k1", "k2"]})
    app = create_app(settings, http_client=keyring.client(), capabilities=NO_SANDBOX)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
            headers = auth_headers(keyring, "a")
            response = await client.get("/v1/environments", headers=headers)
            assert response.status_code == 401 and problem(response)["header"] == "X-API-Key"
            response = await client.get("/v1/environments", headers={**headers, "X-API-Key": "k2"})
            assert response.status_code == 200


async def test_environment_lifecycle(client: httpx.AsyncClient, keyring: FakeKeyring) -> None:
    alice = auth_headers(keyring, "alice")
    env = await create_env(client, alice, labels={"team": "x"}, credentials=["github", "github"])
    assert env["profile"] == "personal" and env["credentials"] == ["github"]
    work = await create_env(client, auth_headers(keyring, "alice", "work"))
    assert work["profile"] == "work"
    response = await client.get("/v1/environments", headers=alice)
    assert {e["id"] for e in response.json()["environments"]} == {env["id"], work["id"]}
    response = await client.get("/v1/environments", params={"profile": "work"}, headers=alice)
    assert [e["id"] for e in response.json()["environments"]] == [work["id"]]
    response = await client.get("/v1/environments", params={"label": "team=x"}, headers=alice)
    assert [e["id"] for e in response.json()["environments"]] == [env["id"]]
    response = await client.get(f"/v1/environments/{env['id']}", headers=alice)
    assert response.json()["name"] == "env"
    response = await client.get(f"/v1/environments/{env['id']}/usage", headers=alice)
    assert response.json()["disk"]["total_bytes"] == 0
    response = await client.get(f"/v1/environments/{env['id']}/summary", headers=alice)
    assert response.json()["environment"]["id"] == env["id"]
    response = await client.post(f"/v1/environments/{env['id']}/reset", headers=alice)
    assert response.status_code == 200
    response = await client.delete(f"/v1/environments/{env['id']}", headers=alice)
    assert response.status_code == 204
    response = await client.get(f"/v1/environments/{env['id']}", headers=alice)
    assert response.status_code == 404 and problem(response)["code"] == "not_found"


async def test_isolation_over_http(client: httpx.AsyncClient, keyring: FakeKeyring) -> None:
    alice = auth_headers(keyring, "alice")
    bob = auth_headers(keyring, "bob")
    env = await create_env(client, alice)
    shell = await open_shell(client, alice, env["id"])
    assert (await client.get("/v1/environments", headers=bob)).json()["environments"] == []
    attempts = [
        ("GET", f"/v1/environments/{env['id']}", None),
        ("DELETE", f"/v1/environments/{env['id']}", None),
        ("POST", f"/v1/environments/{env['id']}/shells", {}),
        ("GET", f"/v1/shells/{shell['id']}", None),
        ("GET", f"/v1/shells/{shell['id']}/output", None),
        ("POST", f"/v1/shells/{shell['id']}/exec", {"command": "id"}),
        ("POST", f"/v1/shells/{shell['id']}/signal", {"signal": "KILL"}),
        ("DELETE", f"/v1/shells/{shell['id']}", None),
        ("GET", f"/v1/environments/{env['id']}/processes", None),
        ("POST", f"/v1/environments/{env['id']}/processes/{shell['pid']}/signal", {"signal": 9}),
        ("GET", f"/v1/environments/{env['id']}/files", None),
        ("PUT", f"/v1/environments/{env['id']}/files/content", {"path": "x", "content": "y"}),
        ("POST", "/v1/exec", {"environment_id": env["id"], "command": "id"}),
    ]
    for method, path, body in attempts:
        response = await client.request(method, path, json=body, headers=bob)
        assert response.status_code == 404, (method, path, response.text)
    response = await client.get(f"/v1/shells/{shell['id']}", headers=alice)
    assert response.json()["state"] == "running"


async def test_quota_409_names_limit(client: httpx.AsyncClient, keyring: FakeKeyring) -> None:
    alice = auth_headers(keyring, "alice")
    await create_env(client, alice)
    await create_env(client, alice)
    response = await client.post("/v1/environments", json={"name": "three"}, headers=alice)
    assert response.status_code == 409
    body = problem(response)
    assert body["code"] == "quota_exceeded" and body["limit"] == "max_environments_per_profile"
    assert body["current"] == 2 and body["maximum"] == 2


async def test_shell_exec_poll_wait_close(client: httpx.AsyncClient, keyring: FakeKeyring) -> None:
    alice = auth_headers(keyring, "alice")
    env = await create_env(client, alice)
    shell = await open_shell(client, alice, env["id"], env={"GREETING": "hi"})
    sid = shell["id"]
    response = await client.post(
        f"/v1/shells/{sid}/exec", json={"command": "echo $GREETING", "wait_ms": 5000}, headers=alice
    )
    body = response.json()
    assert body["state"] == "exited" and body["exit_code"] == 0 and body["output"] == "hi\n"
    assert body["credentials_injected"] == [] and body["credentials_missing"] == []
    response = await client.post(
        f"/v1/shells/{sid}/exec", json={"command": "echo slow; sleep 30"}, headers=alice
    )
    slow = response.json()
    assert slow["state"] == "running"
    response = await client.post(f"/v1/shells/{sid}/exec", json={"command": "true"}, headers=alice)
    assert response.status_code == 409 and problem(response)["code"] == "shell_busy"
    response = await client.get(
        f"/v1/shells/{sid}/output",
        params={"cursor": slow["output_start"], "wait_ms": 5000},
        headers=alice,
    )
    out = response.json()
    assert out["data"] == "slow\n" and out["current_command_id"] == slow["id"]
    assert base64.b64decode(out["data_base64"]) == b"slow\n"
    response = await client.post(f"/v1/shells/{sid}/wait", json={"timeout_ms": 50}, headers=alice)
    assert response.json()["status"] == "timed_out"
    response = await client.post(f"/v1/shells/{sid}/signal", json={"signal": "TERM"}, headers=alice)
    assert response.json()["processes_signalled"] >= 1 and response.json()["signal"] == 15
    response = await client.post(f"/v1/shells/{sid}/wait", json={"timeout_ms": 5000}, headers=alice)
    assert (
        response.json()["status"] == "idle" and response.json()["last_command"]["exit_code"] == 143
    )
    response = await client.post(
        f"/v1/shells/{sid}/signal", json={"signal": "SIGWHAT"}, headers=alice
    )
    assert response.status_code == 422
    response = await client.post(f"/v1/shells/{sid}/signal", json={"signal": 9999}, headers=alice)
    assert response.status_code == 422
    response = await client.get(f"/v1/shells/{sid}/commands", headers=alice)
    assert [c["id"] for c in response.json()["commands"]] == [body["id"], slow["id"]]
    response = await client.get(f"/v1/commands/{slow['id']}", headers=alice)
    # bash reports the signalled foreground job ("Terminated") on stderr, merged into output.
    assert (
        response.json()["output"].startswith("slow\n") and not response.json()["output_truncated"]
    )
    response = await client.get(f"/v1/environments/{env['id']}/shells", headers=alice)
    assert [s["id"] for s in response.json()["shells"]] == [sid]
    response = await client.delete(f"/v1/shells/{sid}", headers=alice)
    assert response.json()["state"] == "closed"
    response = await client.post(f"/v1/shells/{sid}/exec", json={"command": "true"}, headers=alice)
    assert response.status_code == 409 and problem(response)["code"] == "shell_not_running"


async def test_stdin_and_pty(client: httpx.AsyncClient, keyring: FakeKeyring) -> None:
    alice = auth_headers(keyring, "alice")
    env = await create_env(client, alice)
    shell = await open_shell(client, alice, env["id"], pty=True)
    sid = shell["id"]
    assert shell["pty"] is True
    response = await client.post(
        f"/v1/shells/{sid}/exec",
        json={"command": "read -r l </dev/tty; echo tty=$l; read p; echo pipe=$p"},
        headers=alice,
    )
    cmd = response.json()
    response = await client.post(
        f"/v1/shells/{sid}/stdin", json={"data": "one\n", "target": "tty"}, headers=alice
    )
    assert response.json()["bytes_written"] == 4
    response = await client.post(
        f"/v1/shells/{sid}/stdin",
        json={"data": base64.b64encode(b"two\n").decode(), "encoding": "base64"},
        headers=alice,
    )
    assert response.json()["bytes_written"] == 4
    response = await client.post(
        f"/v1/shells/{sid}/stdin", json={"data": "!!", "encoding": "base64"}, headers=alice
    )
    assert response.status_code == 422
    response = await client.post(f"/v1/shells/{sid}/wait", json={"timeout_ms": 5000}, headers=alice)
    assert response.json()["status"] == "idle"
    response = await client.get(f"/v1/commands/{cmd['id']}", headers=alice)
    assert response.json()["output"] == "tty=one\r\npipe=two\r\n"


async def test_processes(client: httpx.AsyncClient, keyring: FakeKeyring) -> None:
    alice = auth_headers(keyring, "alice")
    env = await create_env(client, alice)
    shell = await open_shell(client, alice, env["id"])
    sid = shell["id"]
    response = await client.post(
        f"/v1/shells/{sid}/exec", json={"command": "sleep 30 & echo up; wait"}, headers=alice
    )
    cmd = response.json()
    await client.get(
        f"/v1/shells/{sid}/output",
        params={"cursor": cmd["output_start"], "wait_ms": 5000},
        headers=alice,
    )
    deadline = time.monotonic() + 10
    procs: list[dict[str, Any]] = []
    while time.monotonic() < deadline:
        response = await client.get(f"/v1/environments/{env['id']}/processes", headers=alice)
        procs = response.json()["processes"]
        if len(procs) >= 2:
            break
    sleeper = next(p for p in procs if p["cmdline"].startswith("sleep"))
    assert sleeper["shell_id"] == sid and sleeper["ppid"] == shell["pid"]
    response = await client.get(f"/v1/shells/{sid}/processes", headers=alice)
    assert any(p["pid"] == shell["pid"] for p in response.json()["processes"])
    response = await client.post(
        f"/v1/environments/{env['id']}/processes/{os.getpid()}/signal",
        json={"signal": "TERM"},
        headers=alice,
    )
    assert response.status_code == 404
    response = await client.post(
        f"/v1/environments/{env['id']}/processes/{sleeper['pid']}/signal",
        json={"signal": "KILL"},
        headers=alice,
    )
    assert response.json()["shell_id"] == sid
    response = await client.post(f"/v1/shells/{sid}/wait", json={"timeout_ms": 5000}, headers=alice)
    assert response.json()["status"] == "idle"


async def test_files(client: httpx.AsyncClient, keyring: FakeKeyring) -> None:
    alice = auth_headers(keyring, "alice")
    env = await create_env(client, alice)
    eid = env["id"]
    response = await client.put(
        f"/v1/environments/{eid}/files/content",
        json={"path": "a/b.txt", "content": "hello"},
        headers=alice,
    )
    assert response.json()["size"] == 5
    response = await client.get(
        f"/v1/environments/{eid}/files", params={"path": "a"}, headers=alice
    )
    assert response.json()["entries"][0]["path"] == "a/b.txt"
    response = await client.get(
        f"/v1/environments/{eid}/files/content",
        params={"path": "a/b.txt", "max_bytes": 2},
        headers=alice,
    )
    assert response.json()["content"] == "he" and response.json()["truncated"]
    response = await client.get(
        f"/v1/environments/{eid}/files/content", params={"path": "../../etc/passwd"}, headers=alice
    )
    assert response.status_code == 400 and problem(response)["code"] == "path_outside_workspace"
    response = await client.put(
        f"/v1/environments/{eid}/files/content",
        json={"path": "/etc/evil", "content": "x"},
        headers=alice,
    )
    assert response.status_code == 400
    response = await client.get(
        f"/v1/environments/{eid}/files/content", params={"path": "missing"}, headers=alice
    )
    assert response.status_code == 404
    shell = await open_shell(client, alice, eid)
    await client.post(
        f"/v1/shells/{shell['id']}/exec",
        json={"command": "ln -s /etc etc-link", "wait_ms": 5000},
        headers=alice,
    )
    response = await client.get(
        f"/v1/environments/{eid}/files", params={"path": "etc-link"}, headers=alice
    )
    assert response.status_code == 400
    response = await client.get(f"/v1/environments/{eid}/files", headers=alice)
    assert {e["name"]: e["kind"] for e in response.json()["entries"]}["etc-link"] == "symlink"


async def test_exec_once(client: httpx.AsyncClient, keyring: FakeKeyring) -> None:
    alice = auth_headers(keyring, "alice")
    keyring.credentials[("personal", "github")] = {"value": "ghp_sekretsekret"}
    env = await create_env(client, alice, credentials=["github", "npm"])
    response = await client.post(
        "/v1/exec",
        json={
            "environment_id": env["id"],
            "command": "echo $GITHUB_TOKEN; echo [$NPM_TOKEN]; exit 3",
        },
        headers=alice,
    )
    body = response.json()
    assert body["exit_code"] == 3 and body["state"] == "exited"
    assert body["output"] == "«redacted:github»\n[]\n"
    assert body["credentials_injected"] == ["github"] and body["credentials_missing"] == ["npm"]
    assert body["shell_state"] == "closed"
    response = await client.get(f"/v1/environments/{env['id']}/shells", headers=alice)
    assert response.json()["shells"][0]["state"] == "closed"
    response = await client.post(
        "/v1/exec",
        json={"environment_id": env["id"], "command": "sleep 30", "timeout_ms": 200},
        headers=alice,
    )
    assert response.json()["state"] == "timed_out"
    keyring.sealed = True
    response = await client.post(
        "/v1/exec", json={"environment_id": env["id"], "command": "true"}, headers=alice
    )
    assert response.status_code == 503 and "sealed" in problem(response)["detail"]


async def test_admin_routes(client: httpx.AsyncClient, keyring: FakeKeyring) -> None:
    alice = auth_headers(keyring, "alice")
    ops = auth_headers(keyring, "ops")
    await create_env(client, alice)
    response = await client.get("/v1/admin/environments", headers=alice)
    assert response.status_code == 403 and problem(response)["code"] == "forbidden"
    response = await client.get("/v1/admin/environments", headers=ops)
    assert len(response.json()["environments"]) == 1
    response = await client.put(
        "/v1/admin/quotas/alice",
        json={"overrides": {"max_environments_per_profile": 1}},
        headers=ops,
    )
    assert response.json()["effective"]["max_environments_per_profile"] == 1
    response = await client.post("/v1/environments", json={"name": "blocked"}, headers=alice)
    assert response.status_code == 409 and problem(response)["maximum"] == 1
    response = await client.put(
        "/v1/admin/quotas/alice", json={"overrides": {"bogus": 1}}, headers=ops
    )
    assert response.status_code == 422
    response = await client.get("/v1/admin/quotas/alice", headers=ops)
    assert response.json()["overrides"] == {"max_environments_per_profile": 1}
    response = await client.get("/v1/admin/audit", params={"account_id": "alice"}, headers=ops)
    events = response.json()["events"]
    assert events and all(e["account_id"] == "alice" for e in events)
    assert events[0]["action"] == "environment.create"


async def test_unexpected_error_is_problem_json(
    client: httpx.AsyncClient, keyring: FakeKeyring
) -> None:
    from app.api import deps

    def boom() -> None:
        raise RuntimeError("kaboom")

    app = client.app  # type: ignore[attr-defined]
    app.dependency_overrides[deps.get_service] = boom
    try:
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as raw:
            response = await raw.get("/v1/environments", headers=auth_headers(keyring, "a"))
    finally:
        app.dependency_overrides.clear()
    assert response.status_code == 500 and problem(response)["code"] == "internal_error"


async def test_restart_over_http(settings: Settings, keyring: FakeKeyring) -> None:
    alice = auth_headers(keyring, "alice")
    app = create_app(settings, http_client=keyring.client(), capabilities=NO_SANDBOX)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as client,
    ):
        env = await create_env(client, alice)
        shell = await open_shell(client, alice, env["id"])
        response = await client.post(
            f"/v1/shells/{shell['id']}/exec",
            json={"command": "echo before", "wait_ms": 5000},
            headers=alice,
        )
        cmd = response.json()
        assert cmd["exit_code"] == 0
    reborn = create_app(settings, http_client=keyring.client(), capabilities=NO_SANDBOX)
    async with (
        reborn.router.lifespan_context(reborn),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=reborn), base_url="http://t") as client,
    ):
        response = await client.get(f"/v1/environments/{env['id']}", headers=alice)
        body = response.json()
        assert body["state"] == "active"
        assert body["shells"][0]["state"] in ("closed", "dead")
        response = await client.get(f"/v1/shells/{shell['id']}", headers=alice)
        assert response.status_code == 404
        response = await client.get(f"/v1/commands/{cmd['id']}", headers=alice)
        assert response.json()["output"] == "before\n"
        fresh = await open_shell(client, alice, env["id"])
        assert fresh["state"] == "running"


def test_signal_parsing() -> None:
    from app.api.schemas import parse_signal

    assert parse_signal("15") == 15 and parse_signal("term") == signal.SIGTERM
    assert parse_signal("SIGKILL") == 9 and parse_signal(2) == 2
    from app.errors import ValidationError

    for bad in ("nope", "SIGNOPE", 0, "99999"):
        with pytest.raises(ValidationError):
            parse_signal(bad)


def test_settings_parsing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ENVAPI_OPERATOR_ACCOUNTS", "a, b ,,c")
    monkeypatch.setenv("ENVAPI_MIN_SANDBOX_TIER", "USER")
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    assert settings.operator_accounts == ["a", "b", "c"] and settings.min_sandbox_tier == "user"
    assert settings.jwks_url == "http://localhost:8000/.well-known/jwks.json"
    monkeypatch.setenv("ENVAPI_MIN_SANDBOX_TIER", "bogus")
    with pytest.raises(ValueError, match="unknown sandbox tier"):
        Settings(_env_file=None)  # type: ignore[call-arg]
    monkeypatch.delenv("ENVAPI_MIN_SANDBOX_TIER")
    assert Settings(_env_file=None, api_keys=["x"]).api_keys == ["x"]  # type: ignore[call-arg]
    with pytest.raises(ValueError, match="comma-separated"):
        Settings(_env_file=None, api_keys=5)  # type: ignore[call-arg,arg-type]
