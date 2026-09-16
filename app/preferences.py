"""What one person has chosen, and what environments-api does when it cannot ask.

The deployment's configuration says how environments-api behaves for everybody.
settings-api holds what each person has chosen within that, and this module is the one
place the two meet: it turns a caller's token into the idle lifetimes stamped on an
environment at create, the per-profile cap a create is held to, and the profile a request
is resolved with when it names none. Nothing is read at startup, and with no settings-api
configured every person gets the configuration as it stands -- exactly what
environments-api did before it read anybody's settings at all.

Three rules shape it.

**A person may narrow a ceiling and never raise it.** Idle lifetimes and the per-profile
cap are clamped to the catalogue bounds and to this deployment's ``ENVAPI_`` values. A
person cannot raise ``max_environments_per_account`` or any sandbox exposure quota;
those stay with the operator. ``default_shell`` is in the catalogue and unread here:
there is still one deployment-wide ``ENVAPI_SHELL_BINARY``.

**Resolve at create, not at reap.** The reaper has no user token. Idle TTLs are stamped
on the environment record when it is created, and the reaper reads the record. A later
change applies to environments created afterwards.

**An outage degrades per setting.** The ``environments`` entries fall back to a default,
and when settings-api has never answered, the configuration is that default. The
exception is ``common.default_profile``, which refuses, because guessing ``personal``
would quietly act on the wrong account. It is only read when a request needs a profile
and named none.

**A refusal is not an outage.** settings-api answering 401 or 403 means this service is
misconfigured, and serving defaults would hide that behind behaviour that happens to
work. The request fails instead.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

import structlog
from settings_client import (
    HttpSettingsClient,
    SettingsRefused,
    SettingsRejected,
    SettingsUnavailable,
)

from app.errors import PreferencesUnavailableError

if TYPE_CHECKING:
    from settings_client import ResolvedSettings, SettingsClient

    from app.settings import Settings

log = structlog.get_logger(__name__)

NAMESPACE = "environments"
SECONDS_PER_HOUR = 3_600
SECONDS_PER_MINUTE = 60
IDLE_ENVIRONMENT_HOURS_MIN = 1
IDLE_ENVIRONMENT_HOURS_MAX = 168
IDLE_SHELL_MINUTES_MIN = 1
IDLE_SHELL_MINUTES_MAX = 1_440
MAX_ENVIRONMENTS_PER_PROFILE_MIN = 1
MAX_ENVIRONMENTS_PER_PROFILE_MAX = 5

PROFILE_UNKNOWN = (
    "your default profile could not be read from settings-api; name a profile in the "
    "request, or try again shortly"
)
REFUSED = "settings-api did not accept this service's request for your settings"
NOT_GUESSED = "one of your settings could not be read from settings-api and must not be guessed"


@dataclass(frozen=True, slots=True)
class Preferences:
    """One person's choices, as this service applies them to one create."""

    environment_idle_ttl_seconds: float
    """How long a quiet environment this person creates may sit before it is archived."""

    shell_idle_ttl_seconds: float
    """How long a quiet shell in that environment may sit before it is closed."""

    max_environments_per_profile: int
    """How many environments this person may keep in one profile, inside the operator cap."""

    default_profile: str | None
    """The profile a request uses when it names none.

    ``None`` when settings-api could not be asked and the answer must not be guessed.
    """

    def profile(self, requested: str | None) -> str:
        """The profile a request is resolved with: the one it named, or the default.

        Raises:
            PreferencesUnavailableError: the request named none and the default is unknown.
        """
        if requested is not None:
            return requested
        if self.default_profile is None:
            raise PreferencesUnavailableError(PROFILE_UNKNOWN)
        return self.default_profile


class PreferenceSource(Protocol):
    """Where a request's preferences come from."""

    async def for_token(self, user_token: str | None, /) -> Preferences:
        """The preferences of whoever ``user_token`` belongs to.

        ``None`` is no caller at all -- a path that does not authenticate -- and gets the
        configuration.

        Raises:
            PreferencesUnavailableError: settings-api refused this service, or cannot
                read a setting that must not be guessed.
        """
        ...

    async def aclose(self) -> None:
        """Release whatever this holds open."""
        ...


def deployment_preferences(settings: Settings) -> Preferences:
    """What everybody gets when nobody's own choices are known: the configuration as it is."""
    return Preferences(
        environment_idle_ttl_seconds=settings.environment_idle_ttl_seconds,
        shell_idle_ttl_seconds=settings.shell_idle_ttl_seconds,
        max_environments_per_profile=settings.max_environments_per_profile,
        default_profile=settings.default_profile,
    )


class DeploymentPreferences:
    """Everybody gets the configuration: what environments-api did before settings-api."""

    def __init__(self, settings: Settings) -> None:
        """Bind the configuration everybody gets."""
        self._preferences = deployment_preferences(settings)

    async def for_token(self, _user_token: str | None, /) -> Preferences:
        """Return the configuration; the token is ignored."""
        return self._preferences

    async def aclose(self) -> None:
        """Nothing is held open."""


class SettingsApiPreferences:
    """Each person's own choices, read from settings-api, inside the deployment's ceilings."""

    def __init__(self, *, client: SettingsClient, settings: Settings) -> None:
        """Bind the client and the deployment ceilings it is read inside."""
        self._client = client
        self._settings = settings
        self._deployment = deployment_preferences(settings)

    async def for_token(self, user_token: str | None, /) -> Preferences:
        """Read this person's ``environments`` settings, or the configuration if there is none."""
        if user_token is None:
            return self._deployment

        try:
            resolved = await self._client.resolve(NAMESPACE, user_token=user_token)
        except SettingsUnavailable:
            # Never answered, so not even settings-api's own defaults are known. The
            # configuration stands in for every key that falls back; the one that refuses
            # stays unknown, and is only a problem for a request that needs it.
            log.warning("settings_unavailable", namespace=NAMESPACE)
            return Preferences(
                environment_idle_ttl_seconds=self._deployment.environment_idle_ttl_seconds,
                shell_idle_ttl_seconds=self._deployment.shell_idle_ttl_seconds,
                max_environments_per_profile=self._deployment.max_environments_per_profile,
                default_profile=None,
            )
        except SettingsRejected as error:
            # The status only: settings-api's own detail names grants and namespaces, which
            # an operator reads in its log rather than a caller reading it in ours.
            log.warning("settings_rejected", namespace=NAMESPACE, status_code=error.status_code)
            raise PreferencesUnavailableError(REFUSED) from error

        if resolved.stale:
            log.info("settings_stale", namespace=NAMESPACE)

        try:
            return self._apply(resolved)
        except SettingsRefused as error:
            # No ``environments`` entry this service reads refuses today. If one ever does,
            # carrying on with the configuration in its place is exactly the guess that
            # flag exists to prevent.
            log.warning("settings_refused", namespace=NAMESPACE, key=error.key)
            raise PreferencesUnavailableError(NOT_GUESSED) from error

    async def aclose(self) -> None:
        """Close the settings-api client."""
        await self._client.aclose()

    def _apply(self, resolved: ResolvedSettings) -> Preferences:
        """Turn one person's resolved namespace into what a create is stamped with."""
        deployment = self._deployment
        env_hours = _whole_number(resolved, "idle_environment_hours", minimum=1)
        shell_minutes = _whole_number(resolved, "idle_shell_minutes", minimum=1)
        per_profile = _whole_number(resolved, "max_environments_per_profile", minimum=1)
        return Preferences(
            environment_idle_ttl_seconds=_narrow_ttl(
                deployment.environment_idle_ttl_seconds,
                env_hours,
                unit_seconds=SECONDS_PER_HOUR,
                minimum_units=IDLE_ENVIRONMENT_HOURS_MIN,
                maximum_units=IDLE_ENVIRONMENT_HOURS_MAX,
            ),
            shell_idle_ttl_seconds=_narrow_ttl(
                deployment.shell_idle_ttl_seconds,
                shell_minutes,
                unit_seconds=SECONDS_PER_MINUTE,
                minimum_units=IDLE_SHELL_MINUTES_MIN,
                maximum_units=IDLE_SHELL_MINUTES_MAX,
            ),
            max_environments_per_profile=_narrow_count(
                deployment.max_environments_per_profile,
                per_profile,
                minimum=MAX_ENVIRONMENTS_PER_PROFILE_MIN,
                maximum=MAX_ENVIRONMENTS_PER_PROFILE_MAX,
            ),
            default_profile=self._default_profile(resolved),
        )

    def _default_profile(self, resolved: ResolvedSettings) -> str | None:
        """``common.default_profile``: this person's, the configuration's, or unknown."""
        # Membership is not a read: a refused key raises only when its value is asked for,
        # and this person's default is asked for only when a request needs a profile and
        # named none. Carrying "unknown" here lets everything else proceed.
        if "default_profile" in resolved.refused:
            return None
        value = resolved.get("default_profile", None)
        if isinstance(value, str) and value:
            return value
        if value is not None:
            log.warning("setting_unusable", namespace="common", key="default_profile")
        return self._settings.default_profile


def build_preference_source(
    settings: Settings, *, client: SettingsClient | None = None
) -> PreferenceSource:
    """Choose where preferences come from, and say which in the log.

    Args:
        settings: the configuration, which also says whether settings-api is in use.
        client: substituted by tests with :class:`settings_client.testing.FakeSettingsClient`,
            and used in place of building one from ``settings``.
    """
    if client is None:
        configured = settings.settings_api
        if configured is None:
            log.info("per_person_settings_off")
            return DeploymentPreferences(settings)
        base_url, token = configured
        client = HttpSettingsClient(base_url=base_url, service_token=token.get_secret_value())

    log.info("per_person_settings_on", namespace=NAMESPACE)
    return SettingsApiPreferences(client=client, settings=settings)


def _narrow_ttl(
    deployment: float,
    chosen_units: int | None,
    *,
    unit_seconds: int,
    minimum_units: int,
    maximum_units: int,
) -> float:
    """The deployment's idle TTL, or the person's if they asked for less.

    Catalogue bounds are applied first so a value settings-api should not have stored
    cannot outrun them; the deployment then caps any raise.
    """
    if chosen_units is None:
        return deployment
    seconds = max(minimum_units, min(chosen_units, maximum_units)) * unit_seconds
    return min(deployment, seconds)


def _narrow_count(deployment: int, chosen: int | None, *, minimum: int, maximum: int) -> int:
    """The deployment's cap, or the person's if they asked for less."""
    if chosen is None:
        return deployment
    return min(deployment, max(minimum, min(chosen, maximum)))


def _whole_number(resolved: ResolvedSettings, key: str, *, minimum: int) -> int | None:
    """``key`` as a whole number no smaller than ``minimum``, or ``None`` if there is none.

    A deployment running an older settings-api may not have the key, and a value of the
    wrong shape is settings-api's bug rather than a reason to fail somebody's create.
    Either way the configuration stands in. The key is logged; the value never is.
    """
    value = resolved.get(key, None)
    if isinstance(value, int) and not isinstance(value, bool) and value >= minimum:
        return value
    if value is not None:
        log.warning("setting_unusable", namespace=NAMESPACE, key=key)
    return None


__all__ = [
    "NOT_GUESSED",
    "PROFILE_UNKNOWN",
    "REFUSED",
    "DeploymentPreferences",
    "PreferenceSource",
    "Preferences",
    "SettingsApiPreferences",
    "build_preference_source",
    "deployment_preferences",
]
