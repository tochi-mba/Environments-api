"""Configuration, read from ``ENVAPI_``-prefixed environment variables.

A deployment finds out at startup, not at the first request, when its configuration cannot
mean what it says. :func:`load_settings` refuses an ``ENVAPI_`` variable that names no setting,
because a typo otherwise leaves the default silently in place, and the keyring settings are
held to what keyring itself accepts. Neither refusal repeats a value: the value a typo was
aimed at is as likely as not the service token.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any, Self

from keyring_client import ExactAudience, check_service_token
from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

from app.constants import SandboxTier

if TYPE_CHECKING:
    from collections.abc import Mapping

ENV_PREFIX = "ENVAPI_"
GIB = 1024**3
MIB = 1024**2

NOT_SETTINGS = frozenset({"ENVAPI_ENVIRONMENT_ID", "ENVAPI_URL"})
"""``ENVAPI_`` names that are not settings but may be set where the service runs: this service
puts ``ENVAPI_ENVIRONMENT_ID`` in every shell it starts, and ``scripts/smoke.sh`` reads
``ENVAPI_URL``. Neither is a typo, and neither may stop the service starting."""


def _split_csv(value: object) -> list[str]:
    if isinstance(value, str):
        return [item.strip() for item in value.split(",") if item.strip()]
    if isinstance(value, list):
        return [str(item) for item in value]
    raise ValueError("expected a comma-separated string")


class Settings(BaseSettings):
    """Every tunable, with the deployment-wide quota defaults."""

    # Inputs stay out of validation errors: the input that fails is often a credential.
    model_config = SettingsConfigDict(
        env_prefix=ENV_PREFIX, env_file=".env", extra="forbid", hide_input_in_errors=True
    )

    root: Path = Path("./data")
    keyring_base_url: str = "http://127.0.0.1:8001"
    # Who signs tokens, not where their keys are fetched; behind a proxy the two differ.
    # Must equal keyring's KEYRING_ISSUER exactly.
    keyring_issuer: str = "http://127.0.0.1:8001"
    # Empty is allowed: tokens still verify, and only an environment that declares
    # credentials calls keyring's internal endpoint, which then refuses the call.
    keyring_service_token: SecretStr = SecretStr("")
    # The audience every user token must carry. Keyring's internal endpoint requires it to be
    # this service's name in KEYRING_SERVICE_TOKENS, so change both or neither.
    keyring_service_name: str = "environments-api"
    keyring_timeout_seconds: float = 5.0
    default_profile: str = "personal"
    settings_api_base_url: str | None = None
    """Where settings-api is. Unset, every person gets this configuration as it stands.

    Set, each create reads its owner's ``environments`` settings: idle lifetimes stamped
    on the record, the per-profile cap, and which profile they mean when they name none.
    The ceilings in this configuration still apply on top of what anybody chooses.
    """
    settings_api_token: SecretStr | None = None
    """This service's entry in settings-api's ``SETTINGS_API_SERVICES``.

    At least 32 characters, the rule settings-api enforces on its side. Its grant there
    needs ``audience_prefix`` ``environments-api``: settings-api is shown the same user
    token keyring minted.
    """
    jwks_cache_seconds: float = Field(default=3600.0, gt=0, le=86_400)
    # The least time between two key fetches an unknown kid may provoke, and between a failed
    # fetch and the next. Without it every invented kid is a request to keyring.
    jwks_min_refetch_seconds: float = Field(default=60.0, gt=0, le=3600)

    min_sandbox_tier: str = "directory"
    allow_network: bool = True
    shell_binary: str = "/bin/bash"
    operator_accounts: Annotated[list[str], NoDecode] = Field(default_factory=list)
    api_keys: Annotated[list[str], NoDecode] = Field(default_factory=list)
    log_json: bool = True
    log_level: str = "INFO"

    max_environments_per_profile: int = 5
    max_environments_per_account: int = 20
    max_shells_per_environment: int = 8
    max_processes_per_shell: int = 256
    max_memory_bytes: int = 2 * GIB
    max_file_size_bytes: int = 512 * MIB
    max_cpu_seconds: int = 900
    max_disk_bytes: int = 5 * GIB
    max_output_buffer_bytes: int = 1 * MIB
    max_command_log_bytes: int = 32 * MIB
    environment_idle_ttl_seconds: float = 24 * 3600
    shell_idle_ttl_seconds: float = 3600
    reaper_interval_seconds: float = 60.0
    shell_close_grace_seconds: float = 5.0
    max_file_read_bytes: int = 1 * MIB
    max_file_write_bytes: int = 16 * MIB

    @field_validator("operator_accounts", "api_keys", mode="before")
    @classmethod
    def _csv(cls, value: object) -> list[str]:
        return _split_csv(value)

    @field_validator("min_sandbox_tier")
    @classmethod
    def _tier(cls, value: str) -> str:
        return SandboxTier.parse(value).label

    @field_validator("keyring_service_token")
    @classmethod
    def _service_token(cls, value: SecretStr) -> SecretStr:
        if value.get_secret_value():
            check_service_token(value.get_secret_value())
        return value

    @field_validator("keyring_service_name")
    @classmethod
    def _audience(cls, value: str) -> str:
        return ExactAudience(value).name

    @field_validator("settings_api_base_url")
    @classmethod
    def _blank_settings_url_is_unset(cls, value: str | None) -> str | None:
        """``ENVAPI_SETTINGS_API_BASE_URL=`` in a ``.env`` means off, not an empty URL."""
        return value or None

    @field_validator("settings_api_token", mode="before")
    @classmethod
    def _blank_settings_token_is_unset(cls, value: Any) -> Any:
        if value is None or value == "":
            return None
        if isinstance(value, SecretStr) and not value.get_secret_value():
            return None
        return value

    @field_validator("settings_api_token")
    @classmethod
    def _settings_api_token(cls, value: SecretStr | None) -> SecretStr | None:
        if value is not None:
            check_service_token(value.get_secret_value())
        return value

    @model_validator(mode="after")
    def _settings_api_is_whole(self) -> Self:
        """Refuse half a settings-api configuration, and a token that could never work.

        A URL with no token would be refused on every call, and a token with no URL is a
        secret configured for nothing. Either is somebody's mistake, and startup is the
        cheapest place to hear about it.
        """
        if (self.settings_api_base_url is None) != (self.settings_api_token is None):
            raise ValueError("settings_api_base_url and settings_api_token must be set together")
        return self

    @property
    def min_tier(self) -> SandboxTier:
        """The weakest sandbox tier this deployment accepts."""
        return SandboxTier.parse(self.min_sandbox_tier)

    @property
    def settings_api(self) -> tuple[str, SecretStr] | None:
        """Where settings-api is and how to authenticate to it, or ``None`` when unused.

        One value rather than two optional ones, so that nothing downstream has to
        re-establish that the pair is whole: :meth:`_settings_api_is_whole` already
        refused to construct settings where it is not.
        """
        if self.settings_api_base_url is None or self.settings_api_token is None:
            return None
        return self.settings_api_base_url, self.settings_api_token


class UnknownSettingError(ValueError):
    """An ``ENVAPI_`` variable is set that no setting corresponds to."""


def check_for_unknown_env_vars(environ: Mapping[str, str] | None = None) -> None:
    """Fail on a misspelled setting instead of quietly running with its default.

    Names are compared case-insensitively, as pydantic-settings reads them, so a lower-case
    typo is caught like any other.

    Raises:
        UnknownSettingError: naming every offender at once, so a deployment is fixed in one
            pass. Values are never repeated.
    """
    present = os.environ if environ is None else environ
    known = {ENV_PREFIX + name.upper() for name in Settings.model_fields} | NOT_SETTINGS
    offenders = sorted(
        name
        for name in present
        if name.upper().startswith(ENV_PREFIX) and name.upper() not in known
    )
    if offenders:
        raise UnknownSettingError(
            f"unrecognised configuration: {', '.join(offenders)}. Each setting is {ENV_PREFIX} "
            "followed by a field name from app/settings.py."
        )


def load_settings() -> Settings:
    """Settings from the process environment and ``.env``, once the environment is checked.

    Raises:
        UnknownSettingError: see :func:`check_for_unknown_env_vars`.
    """
    check_for_unknown_env_vars()
    return Settings()
