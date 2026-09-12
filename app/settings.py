"""Configuration, read from ``ENVAPI_``-prefixed environment variables."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

from app.constants import SandboxTier

GIB = 1024**3
MIB = 1024**2


def _split_csv(value: object) -> list[str]:
    if isinstance(value, str):
        return [item.strip() for item in value.split(",") if item.strip()]
    if isinstance(value, list):
        return [str(item) for item in value]
    raise ValueError("expected a comma-separated string")


class Settings(BaseSettings):
    """Every tunable, with the deployment-wide quota defaults."""

    model_config = SettingsConfigDict(env_prefix="ENVAPI_", env_file=".env", extra="forbid")

    root: Path = Path("./data")
    keyring_base_url: str = "http://localhost:8000"
    keyring_service_token: str = ""
    keyring_service_name: str = "environments-api"
    keyring_timeout_seconds: float = 5.0
    default_profile: str = "personal"
    jwks_cache_seconds: float = 300.0

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

    @property
    def min_tier(self) -> SandboxTier:
        """The weakest sandbox tier this deployment accepts."""
        return SandboxTier.parse(self.min_sandbox_tier)

    @property
    def jwks_url(self) -> str:
        """Where keyring publishes its signing keys."""
        return self.keyring_base_url.rstrip("/") + "/.well-known/jwks.json"
