"""One person's choices, and what this service does with them -- and without them.

Needs no host capability and no conftest fixture, so it runs on any workstation.
Every test that reads settings-api here uses its shared fake, including with it switched
off, because the outage is the case most services forget and the one their users notice.
"""

from __future__ import annotations

from typing import Any

import pytest
from settings_client import Fallback, OnUnavailable
from settings_client.testing import FakeSettingsClient
from structlog.testing import capture_logs

from app.environments.models import EnvironmentRecord
from app.errors import PreferencesUnavailableError
from app.logging import configure_logging
from app.preferences import (
    NOT_GUESSED,
    PROFILE_UNKNOWN,
    REFUSED,
    DeploymentPreferences,
    Preferences,
    SettingsApiPreferences,
    build_preference_source,
    deployment_preferences,
)
from app.settings import Settings

USER_TOKEN = "a-user-token-from-keyring"
SETTINGS_API_TOKEN = "settings-api-token-for-environments-01"
HOUR = 3_600
MINUTE = 60

FALLBACKS = {
    "idle_environment_hours": Fallback(default=24, on_unavailable=OnUnavailable.USE_DEFAULT),
    "idle_shell_minutes": Fallback(default=60, on_unavailable=OnUnavailable.USE_DEFAULT),
    "max_environments_per_profile": Fallback(default=5, on_unavailable=OnUnavailable.USE_DEFAULT),
    "default_profile": Fallback(default="personal", on_unavailable=OnUnavailable.REFUSE),
}


@pytest.fixture(autouse=True)
def _logs() -> None:
    configure_logging(json_output=False, level="INFO")


def settings_with(**overrides: Any) -> Settings:
    return Settings(_env_file=None, log_json=False, **overrides)  # type: ignore[call-arg]


def reading(client: FakeSettingsClient, **overrides: Any) -> SettingsApiPreferences:
    return SettingsApiPreferences(client=client, settings=settings_with(**overrides))


class TestWithoutSettingsApi:
    def test_the_configuration_is_what_everybody_gets(self) -> None:
        settings = settings_with(
            environment_idle_ttl_seconds=600,
            shell_idle_ttl_seconds=60,
            max_environments_per_profile=2,
            default_profile="work",
        )

        assert deployment_preferences(settings) == Preferences(
            environment_idle_ttl_seconds=600,
            shell_idle_ttl_seconds=60,
            max_environments_per_profile=2,
            default_profile="work",
        )

    async def test_nobody_is_asked_when_settings_api_is_not_configured(self) -> None:
        settings = settings_with()
        source = build_preference_source(settings)

        assert isinstance(source, DeploymentPreferences)
        assert await source.for_token(USER_TOKEN) == deployment_preferences(settings)
        await source.aclose()

    async def test_a_configured_settings_api_is_asked_per_person(self) -> None:
        source = build_preference_source(
            settings_with(
                settings_api_base_url="https://settings.test",
                settings_api_token=SETTINGS_API_TOKEN,
            )
        )

        assert isinstance(source, SettingsApiPreferences)
        await source.aclose()

    async def test_a_substituted_client_is_the_one_asked(self) -> None:
        client = FakeSettingsClient()

        await build_preference_source(settings_with(), client=client).for_token(USER_TOKEN)

        assert client.resolves == 1


class TestAPersonsChoices:
    async def test_they_become_the_limits_their_environments_are_stamped_with(self) -> None:
        client = FakeSettingsClient()
        client.seed(
            "environments",
            {
                "idle_environment_hours": 2,
                "idle_shell_minutes": 5,
                "max_environments_per_profile": 1,
                "default_profile": "work",
            },
        )

        preferences = await reading(client).for_token(USER_TOKEN)

        assert preferences == Preferences(
            environment_idle_ttl_seconds=2 * HOUR,
            shell_idle_ttl_seconds=5 * MINUTE,
            max_environments_per_profile=1,
            default_profile="work",
        )

    async def test_a_ceiling_can_be_narrowed_and_never_raised(self) -> None:
        client = FakeSettingsClient()
        client.seed(
            "environments",
            {
                "idle_environment_hours": 48,
                "idle_shell_minutes": 120,
                "max_environments_per_profile": 5,
            },
        )

        preferences = await reading(
            client,
            environment_idle_ttl_seconds=HOUR,
            shell_idle_ttl_seconds=10 * MINUTE,
            max_environments_per_profile=2,
        ).for_token(USER_TOKEN)

        assert preferences.environment_idle_ttl_seconds == HOUR
        assert preferences.shell_idle_ttl_seconds == 10 * MINUTE
        assert preferences.max_environments_per_profile == 2

    async def test_catalogue_bounds_cap_a_value_settings_api_should_not_have_stored(self) -> None:
        client = FakeSettingsClient()
        client.seed(
            "environments",
            {
                "idle_environment_hours": 200,
                "idle_shell_minutes": 2_000,
                "max_environments_per_profile": 20,
            },
        )

        preferences = await reading(
            client,
            environment_idle_ttl_seconds=400 * HOUR,
            shell_idle_ttl_seconds=2_000 * MINUTE,
            max_environments_per_profile=20,
        ).for_token(USER_TOKEN)

        assert preferences.environment_idle_ttl_seconds == 168 * HOUR
        assert preferences.shell_idle_ttl_seconds == 1_440 * MINUTE
        assert preferences.max_environments_per_profile == 5

    async def test_a_request_with_no_caller_asks_nobody(self) -> None:
        client = FakeSettingsClient()
        settings = settings_with()

        preferences = await SettingsApiPreferences(client=client, settings=settings).for_token(None)

        assert preferences == deployment_preferences(settings)
        assert client.resolves == 0


class TestWhenSettingsApiCannotBeReached:
    async def test_never_having_answered_leaves_the_configuration_and_an_unknown_profile(
        self,
    ) -> None:
        client = FakeSettingsClient()
        client.unavailable = True
        settings = settings_with()

        preferences = await SettingsApiPreferences(client=client, settings=settings).for_token(
            USER_TOKEN
        )

        assert preferences.environment_idle_ttl_seconds == settings.environment_idle_ttl_seconds
        assert preferences.shell_idle_ttl_seconds == settings.shell_idle_ttl_seconds
        assert preferences.max_environments_per_profile == settings.max_environments_per_profile
        assert preferences.default_profile is None

    async def test_known_fallbacks_are_used_inside_the_deployment_ceilings(self) -> None:
        client = FakeSettingsClient(fallbacks={"environments": FALLBACKS})
        client.unavailable = True

        preferences = await reading(
            client, environment_idle_ttl_seconds=12 * HOUR, max_environments_per_profile=2
        ).for_token(USER_TOKEN)

        assert preferences.environment_idle_ttl_seconds == 12 * HOUR
        assert preferences.shell_idle_ttl_seconds == 60 * MINUTE
        assert preferences.max_environments_per_profile == 2
        assert preferences.default_profile is None

    async def test_an_environments_setting_that_refuses_fails_rather_than_being_guessed(
        self,
    ) -> None:
        refusing = {
            "idle_environment_hours": Fallback(default=24, on_unavailable=OnUnavailable.REFUSE)
        }
        client = FakeSettingsClient(fallbacks={"environments": refusing})
        client.unavailable = True

        with pytest.raises(PreferencesUnavailableError, match=NOT_GUESSED):
            await reading(client).for_token(USER_TOKEN)


class TestWhenSettingsApiRefusesThisService:
    async def test_the_refusal_is_not_hidden_behind_defaults(self) -> None:
        client = FakeSettingsClient()
        client.rejects["environments"] = (403, "environments-api was not granted environments")

        with pytest.raises(PreferencesUnavailableError) as caught:
            await reading(client).for_token(USER_TOKEN)

        assert str(caught.value) == REFUSED
        assert "granted" not in str(caught.value)

    async def test_the_refusal_logs_the_status_and_never_the_detail(self) -> None:
        client = FakeSettingsClient()
        client.rejects["environments"] = (403, "environments-api was not granted environments")

        with capture_logs() as logs, pytest.raises(PreferencesUnavailableError):
            await reading(client).for_token(USER_TOKEN)

        assert any(entry.get("status_code") == 403 for entry in logs)
        assert all("granted" not in str(entry) for entry in logs)
        assert all(USER_TOKEN not in str(entry) for entry in logs)


class TestValuesThatCannotBeUsed:
    @pytest.mark.parametrize("value", [True, "3", 0, -1, None])
    async def test_an_unusable_idle_lifetime_leaves_the_configuration(self, value: Any) -> None:
        client = FakeSettingsClient()
        client.seed("environments", {"idle_environment_hours": value})

        preferences = await reading(client, environment_idle_ttl_seconds=900).for_token(USER_TOKEN)

        assert preferences.environment_idle_ttl_seconds == 900

    async def test_a_missing_setting_leaves_the_configuration(self) -> None:
        client = FakeSettingsClient()
        client.seed("environments", {})
        settings = settings_with()

        preferences = await SettingsApiPreferences(client=client, settings=settings).for_token(
            USER_TOKEN
        )

        assert preferences == deployment_preferences(settings)

    async def test_the_key_is_logged_and_the_value_never_is(self) -> None:
        client = FakeSettingsClient()
        client.seed("environments", {"idle_environment_hours": "a-value-nobody-should-read"})

        with capture_logs() as logs:
            await reading(client).for_token(USER_TOKEN)

        assert any(entry.get("key") == "idle_environment_hours" for entry in logs)
        assert all("a-value-nobody-should-read" not in str(entry) for entry in logs)
        assert all(USER_TOKEN not in str(entry) for entry in logs)

    async def test_a_profile_that_is_not_a_name_leaves_the_configuration(self) -> None:
        client = FakeSettingsClient()
        client.seed("environments", {"default_profile": 7})

        preferences = await reading(client, default_profile="work").for_token(USER_TOKEN)

        assert preferences.default_profile == "work"

    async def test_an_empty_profile_name_leaves_the_configuration(self) -> None:
        client = FakeSettingsClient()
        client.seed("environments", {"default_profile": ""})

        preferences = await reading(client, default_profile="work").for_token(USER_TOKEN)

        assert preferences.default_profile == "work"


class TestChoosingAProfile:
    def test_a_named_profile_is_used_as_named(self) -> None:
        preferences = Preferences(
            environment_idle_ttl_seconds=HOUR,
            shell_idle_ttl_seconds=MINUTE,
            max_environments_per_profile=1,
            default_profile="personal",
        )

        assert preferences.profile("work") == "work"

    def test_the_default_fills_in_when_none_is_named(self) -> None:
        preferences = Preferences(
            environment_idle_ttl_seconds=HOUR,
            shell_idle_ttl_seconds=MINUTE,
            max_environments_per_profile=1,
            default_profile="personal",
        )

        assert preferences.profile(None) == "personal"

    def test_an_unknown_default_refuses_rather_than_guessing(self) -> None:
        preferences = Preferences(
            environment_idle_ttl_seconds=HOUR,
            shell_idle_ttl_seconds=MINUTE,
            max_environments_per_profile=1,
            default_profile=None,
        )

        with pytest.raises(PreferencesUnavailableError) as caught:
            preferences.profile(None)

        assert str(caught.value) == PROFILE_UNKNOWN

    def test_a_named_profile_needs_no_default(self) -> None:
        preferences = Preferences(
            environment_idle_ttl_seconds=HOUR,
            shell_idle_ttl_seconds=MINUTE,
            max_environments_per_profile=1,
            default_profile=None,
        )

        assert preferences.profile("work") == "work"


class TestStampedRecords:
    def test_missing_fields_keep_the_deployment_fallback(self) -> None:
        record = EnvironmentRecord(
            id="env_old",
            account_id="alice",
            profile="personal",
            name="n",
            sandbox_tier="directory",
            created_at=1,
            updated_at=1,
            last_activity_at=1,
        )

        assert record.environment_idle_ttl(86_400) == 86_400
        assert record.shell_idle_ttl(3_600) == 3_600

    def test_stamped_fields_are_what_the_reaper_reads(self) -> None:
        record = EnvironmentRecord(
            id="env_new",
            account_id="alice",
            profile="personal",
            name="n",
            sandbox_tier="directory",
            created_at=1,
            updated_at=1,
            last_activity_at=1,
            environment_idle_ttl_seconds=60,
            shell_idle_ttl_seconds=15,
        )

        assert record.environment_idle_ttl(86_400) == 60
        assert record.shell_idle_ttl(3_600) == 15
