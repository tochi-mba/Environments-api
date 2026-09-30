"""The files API over HTTP: containment, edits, transfers, audit and the disk quota."""

from __future__ import annotations

import httpx

from app.settings import Settings
from tests.conftest import auth_headers, create_env, open_shell, problem
from tests.fake_keyring import FakeKeyring


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


async def test_delete_and_mkdir_are_audited(
    client: httpx.AsyncClient, keyring: FakeKeyring
) -> None:
    alice = auth_headers(keyring, "alice")
    prefix = f"/v1/environments/{(await create_env(client, alice))['id']}/files"
    await client.post(f"{prefix}/directories", json={"path": "made/deep"}, headers=alice)
    await client.put(f"{prefix}/content", json={"path": "gone.txt", "content": "x"}, headers=alice)
    await client.delete(f"{prefix}/content", params={"path": "gone.txt"}, headers=alice)
    await client.delete(
        f"{prefix}/content", params={"path": "made", "recursive": "true"}, headers=alice
    )
    audit = await client.get(
        "/v1/admin/audit", params={"account_id": "alice"}, headers=auth_headers(keyring, "ops")
    )
    events = [
        (e["action"], e["path"], e.get("recursive"))
        for e in audit.json()["events"]
        if e["action"].startswith("file.")
    ]
    assert events == [
        ("file.mkdir", "made/deep", None),
        ("file.write", "gone.txt", None),
        ("file.delete", "gone.txt", False),
        ("file.delete", "made", True),
    ]


async def test_disk_quota_is_checked_before_a_file_is_written(
    client: httpx.AsyncClient, keyring: FakeKeyring
) -> None:
    alice = auth_headers(keyring, "alice")
    prefix = f"/v1/environments/{(await create_env(client, alice))['id']}/files"
    await client.put(
        "/v1/admin/quotas/alice",
        json={"overrides": {"max_disk_bytes": 10}},
        headers=auth_headers(keyring, "ops"),
    )

    async def refused(response: httpx.Response) -> None:
        body = problem(response)
        assert response.status_code == 409 and body["code"] == "quota_exceeded", response.text
        assert body["limit"] == "max_disk_bytes" and body["maximum"] == 10

    async def names() -> set[str]:
        listing = await client.get(prefix, params={"depth": 5}, headers=alice)
        return {e["path"] for e in listing.json()["entries"]}

    async def content(path: str) -> str:
        read = await client.get(f"{prefix}/content", params={"path": path}, headers=alice)
        text: str = read.json()["content"]
        return text

    # A write that would take usage past the quota is refused, and leaves no file and no
    # parent directory behind.
    await refused(
        await client.put(
            f"{prefix}/content", json={"path": "new/big.txt", "content": "x" * 11}, headers=alice
        )
    )
    assert await names() == set()
    ok = await client.put(
        f"{prefix}/content", json={"path": "f.txt", "content": "x" * 9 + "\n"}, headers=alice
    )
    assert ok.status_code == 200
    # Usage now sits at the quota: any route that would add a byte is refused.
    await refused(
        await client.put(f"{prefix}/content", json={"path": "one", "content": "y"}, headers=alice)
    )
    await refused(
        await client.put(
            f"{prefix}/content",
            json={"path": "f.txt", "content": "y", "mode": "append"},
            headers=alice,
        )
    )
    await refused(
        await client.post(
            f"{prefix}/edit",
            json={"path": "f.txt", "old_string": "\n", "new_string": "\n\n"},
            headers=alice,
        )
    )
    await refused(
        await client.post(
            f"{prefix}/patch",
            json={"path": "f.txt", "patch": f"@@ -1 +1 @@\n-{'x' * 9}\n+{'x' * 11}\n"},
            headers=alice,
        )
    )
    await refused(
        await client.post(
            f"{prefix}/copy", json={"source": "f.txt", "destination": "d/c.txt"}, headers=alice
        )
    )
    assert await names() == {"f.txt"} and await content("f.txt") == "x" * 9 + "\n"
    # What adds nothing is admitted at the quota: a same-size rewrite and a move.
    same = await client.put(
        f"{prefix}/content", json={"path": "f.txt", "content": "z" * 9 + "\n"}, headers=alice
    )
    assert same.status_code == 200
    moved = await client.post(
        f"{prefix}/move", json={"source": "f.txt", "destination": "m/f.txt"}, headers=alice
    )
    assert moved.status_code == 200 and await content("m/f.txt") == "z" * 9 + "\n"


async def test_a_failed_write_gives_its_quota_back(
    client: httpx.AsyncClient, keyring: FakeKeyring
) -> None:
    alice = auth_headers(keyring, "alice")
    prefix = f"/v1/environments/{(await create_env(client, alice))['id']}/files"
    await client.put(
        "/v1/admin/quotas/alice",
        json={"overrides": {"max_disk_bytes": 10}},
        headers=auth_headers(keyring, "ops"),
    )
    for _ in range(3):
        stale = await client.put(
            f"{prefix}/content",
            json={"path": "f.txt", "content": "x" * 6},
            headers={**alice, "If-Match": '"stale"'},
        )
        assert stale.status_code == 412
    ok = await client.put(
        f"{prefix}/content", json={"path": "f.txt", "content": "x" * 10}, headers=alice
    )
    assert ok.status_code == 200, ok.text


async def test_encoded_traversal_is_a_literal_name_or_refused(
    client: httpx.AsyncClient, keyring: FakeKeyring, settings: Settings
) -> None:
    alice = auth_headers(keyring, "alice")
    prefix = f"/v1/environments/{(await create_env(client, alice))['id']}/files"
    # In a JSON body nothing is URL-encoded, so a percent escape is part of the name.
    for path in ("%2e%2e%2F%2e%2e%2Fescape.txt", "%2e%2e/%2e%2e/escape.txt", "a%2Fb"):
        written = await client.put(
            f"{prefix}/content", json={"path": path, "content": path}, headers=alice
        )
        assert written.status_code == 200 and written.json()["path"] == path, written.text
        # httpx encodes "%" as "%25"; the framework's one decode gives back the same name.
        read = await client.get(f"{prefix}/content", params={"path": path}, headers=alice)
        assert read.json()["content"] == path
    moved = await client.post(
        f"{prefix}/move",
        json={"source": "a%2Fb", "destination": "%2e%2e%2F%2e%2e%2Fmoved"},
        headers=alice,
    )
    assert moved.json()["path"] == "%2e%2e%2F%2e%2e%2Fmoved"
    listing = await client.get(prefix, params={"depth": 3}, headers=alice)
    assert {e["path"] for e in listing.json()["entries"]} == {
        "%2e%2e",
        "%2e%2e/%2e%2e",
        "%2e%2e/%2e%2e/escape.txt",
        "%2e%2e%2F%2e%2e%2Fescape.txt",
        "%2e%2e%2F%2e%2e%2Fmoved",
    }
    # Nothing landed outside the workspace, above the data root or beside it.
    escaped = [p for p in settings.root.parent.rglob("*") if "escape" in p.name]
    assert escaped and all("workspace" in p.relative_to(settings.root).parts for p in escaped)
    # A traversal encoded once in the URL is decoded once by the framework and refused.
    for raw in ("%2e%2e%2F%2e%2e%2Fetc%2Fpasswd", "..%2F..%2Fetc%2Fpasswd"):
        response = await client.get(f"{prefix}/content?path={raw}", headers=alice)
        assert response.status_code == 400
        assert problem(response)["code"] == "path_outside_workspace"
    # Encoded twice, it is the literal name "%2e%2e/...", which does not exist here.
    response = await client.get(
        f"{prefix}/content?path=%252e%252e%252F%252e%252e%252Fetc%252Fpasswd", headers=alice
    )
    assert response.status_code == 404 and problem(response)["code"] == "not_found"
