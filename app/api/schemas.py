"""Request bodies. Responses are plain mappings documented in ``docs/api.md``."""

from __future__ import annotations

import signal
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

from app.constants import ENV_VAR_PATTERN, LABEL_PATTERN, SERVICE_PATTERN
from app.environments.models import EnvironmentLimits
from app.errors import ValidationError

MAX_TIMEOUT_MS = 24 * 3600 * 1000
MAX_WAIT_MS = 10 * 60 * 1000


class CreateEnvironmentRequest(BaseModel):
    """``POST /v1/environments``."""

    name: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9][A-Za-z0-9 _.-]{0,63}$")
    labels: dict[str, str] = Field(default_factory=dict)
    credentials: list[str] = Field(default_factory=list)
    network: bool | None = None
    limits: EnvironmentLimits | None = None

    @field_validator("labels")
    @classmethod
    def _labels(cls, value: dict[str, str]) -> dict[str, str]:
        for key, item in value.items():
            if not LABEL_PATTERN.match(key) or not LABEL_PATTERN.match(item):
                raise ValueError(f"label {key!r}={item!r} must match {LABEL_PATTERN.pattern}")
        return value

    @field_validator("credentials")
    @classmethod
    def _credentials(cls, value: list[str]) -> list[str]:
        for service in value:
            if not SERVICE_PATTERN.match(service):
                raise ValueError(
                    f"credential service {service!r} must match {SERVICE_PATTERN.pattern}"
                )
        return sorted(set(value))


class OpenShellRequest(BaseModel):
    """``POST /v1/environments/{id}/shells``."""

    cwd: str = "."
    env: dict[str, str] = Field(default_factory=dict)
    pty: bool = False

    @field_validator("env")
    @classmethod
    def _env(cls, value: dict[str, str]) -> dict[str, str]:
        for key in value:
            if not ENV_VAR_PATTERN.match(key):
                raise ValueError(f"invalid environment variable name {key!r}")
        return value


class ExecRequest(BaseModel):
    """``POST /v1/shells/{sid}/exec``."""

    command: str = Field(min_length=1)
    timeout_ms: int | None = Field(default=None, ge=1, le=MAX_TIMEOUT_MS)
    wait_ms: int = Field(default=0, ge=0, le=MAX_WAIT_MS)


class WaitRequest(BaseModel):
    """``POST /v1/shells/{sid}/wait``."""

    timeout_ms: int = Field(default=30_000, ge=0, le=MAX_WAIT_MS)


class SignalRequest(BaseModel):
    """``POST .../signal``: a number or a name such as ``SIGTERM`` or ``TERM``."""

    signal: int | str = "SIGTERM"

    def number(self) -> int:
        """The signal as an integer.

        Raises:
            ValidationError: For an unknown name or number.
        """
        return parse_signal(self.signal)


def parse_signal(value: int | str) -> int:
    """Turn ``15``, ``"15"``, ``"TERM"`` or ``"SIGTERM"`` into a signal number."""
    if isinstance(value, int) or value.isdigit():
        number = int(value)
        if number in signal.valid_signals():
            return number
        raise ValidationError(f"unknown signal {value!r}", signal=value)
    name = value.upper()
    if not name.startswith("SIG"):
        name = "SIG" + name
    try:
        return int(signal.Signals[name])
    except KeyError as exc:
        raise ValidationError(f"unknown signal {value!r}", signal=value) from exc


class StdinRequest(BaseModel):
    """``POST /v1/shells/{sid}/stdin``."""

    data: str
    encoding: Literal["utf-8", "base64"] = "utf-8"
    target: Literal["stdin", "tty"] = "stdin"


class WriteFileRequest(BaseModel):
    """``PUT /v1/environments/{id}/files/content``."""

    path: str = Field(min_length=1)
    content: str
    encoding: Literal["utf-8", "base64"] = "utf-8"
    mode: Literal["overwrite", "append"] = "overwrite"


class EditFileRequest(BaseModel):
    """Replace one exact occurrence in a UTF-8 file."""

    path: str = Field(min_length=1)
    old_string: str = Field(min_length=1)
    new_string: str


class PatchFileRequest(BaseModel):
    """Apply a unified diff to one named file."""

    path: str = Field(min_length=1)
    patch: str = Field(min_length=1)


class DirectoryRequest(BaseModel):
    """Create one directory and its missing parents."""

    path: str = Field(min_length=1)


class TransferFileRequest(BaseModel):
    """Copy or move one regular file without overwriting its destination."""

    source: str = Field(min_length=1)
    destination: str = Field(min_length=1)


class ExecOnceRequest(BaseModel):
    """``POST /v1/exec``: open an ephemeral shell, run, return, close.

    ``output_window`` says which end of the output ``max_output_bytes`` keeps: ``head``, the
    default and all there used to be, or ``tail``, where a test run or a build prints its
    verdict.
    """

    environment_id: str
    command: str = Field(min_length=1)
    timeout_ms: int = Field(default=60_000, ge=1, le=MAX_WAIT_MS)
    cwd: str = "."
    env: dict[str, str] = Field(default_factory=dict)
    pty: bool = False
    max_output_bytes: int = Field(default=256 * 1024, ge=1, le=8 * 1024 * 1024)
    output_window: Literal["head", "tail"] = "head"


class QuotaOverridesRequest(BaseModel):
    """``PUT /v1/admin/quotas/{account}``."""

    overrides: dict[str, Any] = Field(default_factory=dict)
