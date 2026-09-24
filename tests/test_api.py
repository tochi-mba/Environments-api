from __future__ import annotations

import base64
import os
import signal
import time
from typing import Any

import httpx
import pytest
from keyring_client import BAD_TOKEN, KEYS_STALE, KEYS_UNAVAILABLE
from keyring_client.testing import ROTATED_KEY, FakeClock, forge_hs256, mint

from app.constants import PROBLEM_JSON
from app.main import create_app
from app.preferences import PROFILE_UNKNOWN, REFUSED, SettingsApiPreferences
from app.settings import Settings
from tests.conftest import NO_SANDBOX, auth_headers
from tests.fake_keyring import AUDIENCE, ISSUER, FakeKeyring


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
    assert body["sandbox_tier"] == "directory"
    assert body["keyring"] == {"status": "ok", "error": None} and keyring.fetches == 1
    keyring.error = httpx.ConnectError("keyring is down")
    response = await client.get("/health/ready")
    # Keys fetched a moment ago are still fresh, so keyring is not asked again.
    assert response.status_code == 200 and response.json()["keyring"]["status"] == "ok"
    assert keyring.fetches == 1


async def test_ready_without_keys_is_503(settings: Settings, keyring: FakeKeyring) -> None:
    keyring.error = httpx.ConnectError("refused by https://operator:hunter2@keyring.test")
    app = create_app(settings, keyring_transport=keyring.transport(), capabilities=NO_SANDBOX)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
            response = await client.get("/health/ready")
            assert response.status_code == 503
            assert response.json()["keyring"] == {
                "status": "unreachable",
                "error": KEYS_UNAVAILABLE,
            }
            response = await client.get("/v1/environments", headers=auth_headers(keyring, "a"))
            body = problem(response)
            assert response.status_code == 503 and body["code"] == "keyring_unavailable"
            assert body["detail"] == KEYS_UNAVAILABLE
            assert "hunter2" not in response.text and "keyring.test" not in response.text


async def test_keys_are_served_stale_through_a_keyring_outage(
    settings: Settings, keyring: FakeKeyring
) -> None:
    clock = FakeClock()
    app = create_app(
        settings, keyring_transport=keyring.transport(), clock=clock, capabilities=NO_SANDBOX
    )
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
            alice = auth_headers(keyring, "alice")
            assert (await client.get("/v1/environments", headers=alice)).status_code == 200
            keyring.error = httpx.ConnectError("keyring is down")
            clock.advance(settings.jwks_cache_seconds + 1)
            assert (await client.get("/v1/environments", headers=alice)).status_code == 200
            response = await client.get("/health/ready")
            assert response.status_code == 200
            assert response.json()["keyring"] == {"status": "stale", "error": KEYS_STALE}


async def test_bearer_is_canonical_and_the_legacy_header_still_works(
    client: httpx.AsyncClient, keyring: FakeKeyring
) -> None:
    token = keyring.mint(account_id="alice")
    for headers in (
        {"Authorization": f"Bearer {token}"},
        {"Authorization": f"bearer {token}"},
        {"X-Keyring-User-Token": token},
        {"Authorization": f"Bearer {token}", "X-Keyring-User-Token": token},
    ):
        response = await client.get("/v1/environments", headers=headers)
        assert response.status_code == 200, headers


async def test_every_token_refusal_is_the_same_401(
    client: httpx.AsyncClient, keyring: FakeKeyring
) -> None:
    """A caller learns nothing from which rule refused its token; the log says which."""
    alice = keyring.mint(account_id="alice")
    bob = keyring.mint(account_id="bob")
    foreign = keyring.mint(account_id="alice", audience="web-search-api")
    elsewhere = mint(audience=AUDIENCE, issuer="https://another-keyring.test")
    unpublished = mint(audience=AUDIENCE, issuer=ISSUER, key=ROTATED_KEY)
    refused: list[dict[str, str]] = [
        {},
        {"Authorization": f"Basic {alice}"},
        {"Authorization": "Bearer"},
        {"Authorization": f"Bearer {alice}", "X-Keyring-User-Token": bob},
        {"Authorization": f"Bearer {foreign}"},
        {"Authorization": f"Bearer {elsewhere}"},
        {"Authorization": f"Bearer {forge_hs256(audience=AUDIENCE, issuer=ISSUER)}"},
        # Asked about after keyring's keys were fetched successfully: not an outage, a 401.
        {"Authorization": f"Bearer {unpublished}"},
        {"X-Keyring-User-Token": "not-a-jwt"},
    ]
    bodies = []
    for headers in refused:
        response = await client.get("/v1/environments", headers=headers)
        assert response.status_code == 401, headers
        bodies.append(problem(response))
    assert all(body == bodies[0] for body in bodies)
    assert bodies[0]["detail"] == BAD_TOKEN and "token_error" not in bodies[0]
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
    app = create_app(settings, keyring_transport=keyring.transport(), capabilities=NO_SANDBOX)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
            headers = auth_headers(keyring, "a")
            response = await client.get("/v1/environments", headers=headers)
            assert response.status_code == 401 and problem(response)["header"] == "X-API-Key"
            # Authorization carries the user token and never stands in for an API key.
            response = await client.get("/v1/environments", headers={"Authorization": "Bearer k2"})
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


async def test_safe_file_edit_search_transfer_and_delete_api(
    client: httpx.AsyncClient, keyring: FakeKeyring
) -> None:
    alice = auth_headers(keyring, "alice")
    environment = await create_env(client, alice)
    prefix = f"/v1/environments/{environment['id']}/files"

    written = await client.put(
        f"{prefix}/content",
        json={"path": "src/note.txt", "content": "hello world\n"},
        headers=alice,
    )
    etag = written.headers["etag"]
    assert written.json()["etag"] == etag

    edited = await client.post(
        f"{prefix}/edit",
        json={"path": "src/note.txt", "old_string": "hello", "new_string": "hi"},
        headers={**alice, "If-Match": etag},
    )
    assert edited.status_code == 200
    assert "-hello world" in edited.json()["diff"]
    assert "+hi world" in edited.json()["diff"]
    stale = await client.post(
        f"{prefix}/edit",
        json={"path": "src/note.txt", "old_string": "hi", "new_string": "no"},
        headers={**alice, "If-Match": etag},
    )
    assert stale.status_code == 412 and problem(stale)["code"] == "file_changed"

    patched = await client.post(
        f"{prefix}/patch",
        json={
            "path": "src/note.txt",
            "patch": "@@ -1 +1 @@\n-hi world\n+bye world\n",
        },
        headers={**alice, "If-Match": edited.headers["etag"]},
    )
    assert patched.json()["applied_hunks"] == [1]
    searched = await client.get(
        f"{prefix}/search",
        params={"pattern": "bye", "path": "src", "mode": "content"},
        headers=alice,
    )
    assert searched.json()["matches"][0]["lines"][0]["text"] == "bye world"

    copied = await client.post(
        f"{prefix}/copy",
        json={"source": "src/note.txt", "destination": "copy.txt"},
        headers=alice,
    )
    assert copied.json()["path"] == "copy.txt"
    moved = await client.post(
        f"{prefix}/move",
        json={"source": "copy.txt", "destination": "archive/note.txt"},
        headers=alice,
    )
    assert moved.json()["path"] == "archive/note.txt"
    directory = await client.post(
        f"{prefix}/directories", json={"path": "empty/nested"}, headers=alice
    )
    assert directory.json()["path"] == "empty/nested"

    deleted = await client.delete(
        f"{prefix}/content", params={"path": "archive/note.txt"}, headers=alice
    )
    assert deleted.json()["path"] == "archive/note.txt"
    missing = await client.get(
        f"{prefix}/content", params={"path": "archive/note.txt"}, headers=alice
    )
    assert missing.status_code == 404


async def test_exec_once(client: httpx.AsyncClient, keyring: FakeKeyring) -> None:
    alice = auth_headers(keyring, "alice")
    keyring.connect(
        account_id="alice",
        profile="personal",
        service="github",
        headers={"Authorization": "Bearer ghp_sekretsekret"},
    )
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


async def test_exec_once_echoes_the_command_it_was_sent_not_the_subshell_around_it(
    client: httpx.AsyncClient, keyring: FakeKeyring
) -> None:
    """The bug, named: ``POST /v1/exec`` answered ``"command": "( ls -la\\n)"``.

    The route wraps the caller's command in a subshell so that ``exit N`` cannot take the
    ephemeral shell down with it, and the record it echoed was the wrapped string. The hub
    prefers that echo to its own copy (``src/lucy_api/clients/environments.py:408`` in
    LUCY-assistant), so the model was shown a command it never wrote. The audit log still
    records what the shell actually ran.
    """
    alice = auth_headers(keyring, "alice")
    env = await create_env(client, alice)
    response = await client.post(
        "/v1/exec",
        json={"environment_id": env["id"], "command": "echo hi; exit 3"},
        headers=alice,
    )
    body = response.json()
    assert body["command"] == "echo hi; exit 3"
    assert body["exit_code"] == 3 and body["output"] == "hi\n"
    audit = await client.get(
        "/v1/admin/audit", params={"account_id": "alice"}, headers=auth_headers(keyring, "ops")
    )
    ran = [e for e in audit.json()["events"] if e["action"] == "shell.exec"]
    assert [e["command"] for e in ran] == ["( echo hi; exit 3\n)"]


async def test_exec_once_returns_the_end_of_a_long_output_on_request_and_counts_the_cut(
    client: httpx.AsyncClient, keyring: FakeKeyring
) -> None:
    """The bug, named: everything after the first ``max_output_bytes`` was dropped uncounted.

    The hub asks for 64 KiB (``DEFAULT_OUTPUT_BYTES`` in LUCY-assistant's
    ``src/lucy_api/clients/environments.py``) and reads only ``output`` and
    ``output_dropped_bytes``, which counts ring-buffer evictions and nothing else. A long
    test run came back as its first 64 KiB, without the summary line, and the model was
    told almost nothing had been omitted. The default stays the head, so no caller changes.
    """
    alice = auth_headers(keyring, "alice")
    env = await create_env(client, alice)
    printed = "".join(f"{n}\n" for n in range(1, 301))
    request = {"environment_id": env["id"], "command": "seq 1 300", "max_output_bytes": 100}

    tail = await client.post("/v1/exec", json={**request, "output_window": "tail"}, headers=alice)
    body = tail.json()
    assert body["output"] == printed[-100:]
    assert body["output_truncated_bytes"] == len(printed) - 100
    assert body["output_dropped_bytes"] == 0

    head = await client.post("/v1/exec", json=request, headers=alice)
    body = head.json()
    assert body["output"] == printed[:100]
    assert body["output_truncated_bytes"] == len(printed) - 100

    whole = await client.post(
        "/v1/exec",
        json={**request, "max_output_bytes": 4096, "output_window": "tail"},
        headers=alice,
    )
    assert whole.json()["output"] == printed and whole.json()["output_truncated_bytes"] == 0

    middle = await client.post(
        "/v1/exec", json={**request, "output_window": "middle"}, headers=alice
    )
    assert middle.status_code == 422


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
    app = create_app(settings, keyring_transport=keyring.transport(), capabilities=NO_SANDBOX)
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
    reborn = create_app(settings, keyring_transport=keyring.transport(), capabilities=NO_SANDBOX)
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


SETTINGS_API_TOKEN = "settings-api-token-for-environments-01"
HOUR = 3_600


async def test_create_stamps_the_persons_idle_ttls_and_default_profile(
    settings: Settings, keyring: FakeKeyring
) -> None:
    from settings_client.testing import FakeSettingsClient

    fake = FakeSettingsClient()
    fake.seed(
        "environments",
        {
            "idle_environment_hours": 1,
            "idle_shell_minutes": 5,
            "max_environments_per_profile": 3,
            "default_profile": "work",
        },
    )
    settings = settings.model_copy(
        update={
            "settings_api_base_url": "https://settings.test",
            "settings_api_token": SETTINGS_API_TOKEN,
        }
    )
    app = create_app(
        settings,
        keyring_transport=keyring.transport(),
        capabilities=NO_SANDBOX,
        settings_client=fake,
    )
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as client,
    ):
        response = await client.post(
            "/v1/environments", json={"name": "scratch"}, headers=auth_headers(keyring, "alice")
        )
        assert response.status_code == 201, response.text
        body = response.json()
        assert body["profile"] == "work"
        assert body["environment_idle_ttl_seconds"] == HOUR
        assert body["shell_idle_ttl_seconds"] == 5 * 60


async def test_an_outage_without_a_named_profile_is_503(
    settings: Settings, keyring: FakeKeyring
) -> None:
    from settings_client.testing import FakeSettingsClient

    fake = FakeSettingsClient()
    fake.unavailable = True
    settings = settings.model_copy(
        update={
            "settings_api_base_url": "https://settings.test",
            "settings_api_token": SETTINGS_API_TOKEN,
        }
    )
    app = create_app(
        settings,
        keyring_transport=keyring.transport(),
        capabilities=NO_SANDBOX,
        settings_client=fake,
    )
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as client,
    ):
        response = await client.get("/v1/environments", headers=auth_headers(keyring, "alice"))
        body = problem(response)
        assert response.status_code == 503 and body["code"] == "preferences_unavailable"
        assert body["detail"] == PROFILE_UNKNOWN


async def test_an_outage_with_a_named_profile_stamps_deployment_values(
    settings: Settings, keyring: FakeKeyring
) -> None:
    from settings_client.testing import FakeSettingsClient

    fake = FakeSettingsClient()
    fake.unavailable = True
    settings = settings.model_copy(
        update={
            "settings_api_base_url": "https://settings.test",
            "settings_api_token": SETTINGS_API_TOKEN,
        }
    )
    app = create_app(
        settings,
        keyring_transport=keyring.transport(),
        capabilities=NO_SANDBOX,
        settings_client=fake,
    )
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as client,
    ):
        response = await client.post(
            "/v1/environments",
            json={"name": "scratch"},
            headers=auth_headers(keyring, "alice", "work"),
        )
        assert response.status_code == 201, response.text
        body = response.json()
        assert body["profile"] == "work"
        assert body["environment_idle_ttl_seconds"] == settings.environment_idle_ttl_seconds
        assert body["shell_idle_ttl_seconds"] == settings.shell_idle_ttl_seconds


async def test_settings_api_refusing_this_service_is_a_503(
    settings: Settings, keyring: FakeKeyring
) -> None:
    from settings_client.testing import FakeSettingsClient
    from structlog.testing import capture_logs

    fake = FakeSettingsClient()
    fake.rejects["environments"] = (403, "environments-api was not granted environments")
    settings = settings.model_copy(
        update={
            "settings_api_base_url": "https://settings.test",
            "settings_api_token": SETTINGS_API_TOKEN,
        }
    )
    app = create_app(
        settings,
        keyring_transport=keyring.transport(),
        capabilities=NO_SANDBOX,
        settings_client=fake,
    )
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as client,
    ):
        with capture_logs() as logs:
            response = await client.get(
                "/v1/environments", headers=auth_headers(keyring, "alice", "work")
            )
        body = problem(response)
        assert response.status_code == 503 and body["detail"] == REFUSED
        assert "granted" not in response.text
        assert any(entry.get("status_code") == 403 for entry in logs)
        assert all("granted" not in str(entry) for entry in logs)


async def test_a_settings_client_is_not_asked_at_startup_and_is_closed(
    settings: Settings, keyring: FakeKeyring
) -> None:
    class RecordingClient:
        def __init__(self) -> None:
            self.closed = False
            self.resolves = 0

        async def resolve(self, namespace: str, *, user_token: str) -> Any:
            self.resolves += 1
            raise AssertionError("must not fetch settings at startup")

        async def set(self, namespace: str, key: str, value: object, *, user_token: str) -> int:
            raise AssertionError("must not write settings at startup")

        async def aclose(self) -> None:
            self.closed = True

    recorder = RecordingClient()
    settings = settings.model_copy(
        update={
            "settings_api_base_url": "https://settings.test",
            "settings_api_token": SETTINGS_API_TOKEN,
        }
    )
    app = create_app(
        settings,
        keyring_transport=keyring.transport(),
        capabilities=NO_SANDBOX,
        settings_client=recorder,
    )
    async with app.router.lifespan_context(app):
        assert isinstance(app.state.preferences, SettingsApiPreferences)
        assert recorder.resolves == 0
    assert recorder.closed


async def test_a_bad_token_does_not_ask_settings_api(
    settings: Settings, keyring: FakeKeyring
) -> None:
    from settings_client.testing import FakeSettingsClient

    fake = FakeSettingsClient()
    settings = settings.model_copy(
        update={
            "settings_api_base_url": "https://settings.test",
            "settings_api_token": SETTINGS_API_TOKEN,
        }
    )
    app = create_app(
        settings,
        keyring_transport=keyring.transport(),
        capabilities=NO_SANDBOX,
        settings_client=fake,
    )
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as client,
    ):
        response = await client.get(
            "/v1/environments", headers={"Authorization": "Bearer not-a-token"}
        )
        assert response.status_code == 401
        assert fake.resolves == 0
