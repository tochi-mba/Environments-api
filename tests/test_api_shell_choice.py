"""``environments.default_shell`` over HTTP: the shell a person's sessions start."""

from __future__ import annotations

from collections.abc import AsyncIterator

import httpx
import pytest
from settings_client.testing import FakeSettingsClient

from app.main import create_app
from app.settings import Settings
from tests.conftest import NO_SANDBOX, auth_headers, create_env, open_shell
from tests.fake_keyring import FakeKeyring

SETTINGS_API_TOKEN = "settings-api-token-for-environments-01"


@pytest.fixture
def fake() -> FakeSettingsClient:
    return FakeSettingsClient()


@pytest.fixture
async def http(
    settings: Settings, keyring: FakeKeyring, fake: FakeSettingsClient
) -> AsyncIterator[httpx.AsyncClient]:
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
        yield client


async def test_a_person_who_chose_sh_gets_sh_from_both_ways_of_opening_a_shell(
    http: httpx.AsyncClient, keyring: FakeKeyring, fake: FakeSettingsClient
) -> None:
    """The bug, named: ``environments.default_shell`` was stored and changed no shell."""
    fake.seed("environments", {"default_shell": "sh"})
    alice = auth_headers(keyring, "alice", "personal")
    env = await create_env(http, alice)

    shell = await open_shell(http, alice, env["id"])
    response = await http.post(
        "/v1/exec",
        json={"environment_id": env["id"], "command": "tr '\\0' ' ' < /proc/$$/cmdline"},
        headers=alice,
    )

    assert shell["shell_binary"] == "/bin/sh"
    assert response.status_code == 200, response.text
    assert response.json()["output"] == "/bin/sh "


async def test_a_person_who_chose_bash_or_nothing_gets_the_deployments_shell(
    http: httpx.AsyncClient, keyring: FakeKeyring, fake: FakeSettingsClient, settings: Settings
) -> None:
    alice = auth_headers(keyring, "alice", "personal")
    env = await create_env(http, alice)

    unchosen = await open_shell(http, alice, env["id"])
    fake.seed("environments", {"default_shell": "bash"})
    chosen = await open_shell(http, alice, env["id"])

    assert unchosen["shell_binary"] == chosen["shell_binary"] == settings.shell_binary


async def test_without_settings_api_every_shell_is_the_deployments(
    client: httpx.AsyncClient, keyring: FakeKeyring, settings: Settings
) -> None:
    alice = auth_headers(keyring, "alice")
    env = await create_env(client, alice)

    shell = await open_shell(client, alice, env["id"])

    assert shell["shell_binary"] == settings.shell_binary
