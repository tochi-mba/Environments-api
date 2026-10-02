"""A person's choices are read for the profile the request runs as.

The bug, named: environments-api asked settings-api for the ``environments`` namespace
without naming a profile. The idle lifetimes, the default shell and the command timeout are
profile-scoped, and settings-api returns a profile's values only to a resolve that names it,
so each reached this service as the catalogue default: a person who chose ``sh`` got the
deployment's shell, and shorter idle lifetimes never applied. The shared test fake ignored
the profile too, so no test noticed.
"""

from __future__ import annotations

from typing import Any

from settings_client import Fallback, OnUnavailable
from settings_client.testing import FakeSettingsClient

from app.preferences import SettingsApiPreferences
from app.settings import Settings

TOKEN = "a-user-token-from-keyring"
HOUR = 3_600


def reading(client: FakeSettingsClient, **overrides: Any) -> SettingsApiPreferences:
    settings = Settings(_env_file=None, log_json=False, default_profile="personal", **overrides)  # type: ignore[call-arg]
    return SettingsApiPreferences(client=client, settings=settings)


def chosen_in(profile: str) -> FakeSettingsClient:
    client = FakeSettingsClient()
    client.seed(
        "environments", {"idle_environment_hours": 2, "default_shell": "sh"}, profile=profile
    )
    return client


async def test_a_named_profile_is_the_one_asked_for_and_its_choices_apply() -> None:
    client = chosen_in("family")

    preferences = await reading(client, sh_binary="/bin/sh").for_token(TOKEN, profile="family")

    assert client.asked == [("environments", "family")]
    assert preferences.environment_idle_ttl_seconds == 2 * HOUR
    assert preferences.shell_binary == "/bin/sh"


async def test_with_no_profile_named_the_persons_default_profile_is_asked_for() -> None:
    client = chosen_in("family")
    client.seed("environments", {"default_profile": "family"})

    preferences = await reading(client).for_token(TOKEN)

    assert client.asked == [("environments", None), ("environments", "family")]
    assert preferences.environment_idle_ttl_seconds == 2 * HOUR
    assert preferences.profile(None) == "family"


async def test_with_no_default_chosen_the_deployments_default_profile_is_asked_for() -> None:
    client = chosen_in("personal")

    preferences = await reading(client).for_token(TOKEN)

    assert client.asked == [("environments", None), ("environments", "personal")]
    assert preferences.environment_idle_ttl_seconds == 2 * HOUR


async def test_another_profiles_choices_do_not_apply() -> None:
    client = chosen_in("family")

    preferences = await reading(client, environment_idle_ttl_seconds=24 * HOUR).for_token(
        TOKEN, profile="work"
    )

    assert preferences.environment_idle_ttl_seconds == 24 * HOUR
    assert preferences.shell_binary is None


async def test_a_default_profile_that_cannot_be_read_asks_nothing_more() -> None:
    client = FakeSettingsClient()
    client.unavailable = True
    client.seed_fallback(
        "environments",
        "default_profile",
        Fallback(default="personal", on_unavailable=OnUnavailable.REFUSE),
    )

    preferences = await reading(client).for_token(TOKEN)

    assert client.asked == [("environments", None)]
    assert preferences.default_profile is None
