from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path as _Path

import pytest

from app.sandbox.detect import probe_host

# Coverage data from forked children that dropped privileges lands here; the directory must
# exist and be writable by anyone before those children exit.
_COVERAGE_DIR = _Path(__file__).resolve().parent.parent / ".coverage-data"
_COVERAGE_DIR.mkdir(exist_ok=True)
_COVERAGE_DIR.chmod(0o1777)


def _host_is_root() -> bool:
    return os.geteuid() == 0


@pytest.fixture(scope="session")
def host_caps() -> object:
    return probe_host()


requires_root = pytest.mark.skipif(not _host_is_root(), reason="needs root for the user tier")
requires_useradd = pytest.mark.skipif(
    shutil.which("useradd") is None, reason="needs useradd for the user tier"
)


def _unshare_ok() -> bool:
    caps = probe_host()
    return caps.unshare_works


requires_unshare = pytest.mark.skipif(
    not _unshare_ok(), reason="needs a usable unshare for the namespace tier"
)


class Clock:
    """A settable clock injected into services and shells."""

    def __init__(self) -> None:
        self.now = 1_000_000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def clock() -> Clock:
    return Clock()


def run_quiet(argv: list[str]) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(argv, capture_output=True, check=False, timeout=30)


# ----- app-level fixtures --------------------------------------------------------------

from collections.abc import AsyncIterator  # noqa: E402
from pathlib import Path  # noqa: E402

import httpx  # noqa: E402

from app.main import create_app  # noqa: E402
from app.sandbox.detect import HostCapabilities  # noqa: E402
from app.settings import Settings  # noqa: E402
from tests.fake_keyring import SERVICE_TOKEN, FakeKeyring  # noqa: E402

NO_SANDBOX = HostCapabilities(
    is_root=False, has_useradd=False, has_setpriv=False, unshare_works=False
)


@pytest.fixture
def keyring() -> FakeKeyring:
    return FakeKeyring()


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        _env_file=None,  # type: ignore[call-arg]
        root=tmp_path / "data",
        keyring_base_url="http://keyring",
        keyring_service_token=SERVICE_TOKEN,
        operator_accounts="ops",  # type: ignore[arg-type]
        reaper_interval_seconds=3600,
        shell_close_grace_seconds=1.0,
        max_environments_per_profile=2,
        max_environments_per_account=3,
        max_shells_per_environment=2,
        max_output_buffer_bytes=4096,
        max_command_log_bytes=8192,
        max_file_read_bytes=4096,
        max_file_write_bytes=4096,
        log_json=False,
    )


def auth_headers(keyring: FakeKeyring, account: str, profile: str | None = None) -> dict[str, str]:
    headers = {"X-Keyring-User-Token": keyring.mint(account)}
    if profile:
        headers["X-Keyring-Profile"] = profile
    return headers


@pytest.fixture
async def client(settings: Settings, keyring: FakeKeyring) -> AsyncIterator[httpx.AsyncClient]:
    app = create_app(settings, http_client=keyring.client(), capabilities=NO_SANDBOX)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://envapi") as http:
            http.app = app  # type: ignore[attr-defined]
            yield http
