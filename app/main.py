"""Application factory and process lifecycle."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncGenerator
from typing import TYPE_CHECKING, Any

import structlog
from fastapi import FastAPI
from keyring_client import CredentialClient as SharedCredentialClient
from keyring_client import JwksClient, SystemClock, jwks_url
from keyring_client import TokenVerifier as SharedTokenVerifier

from app.api.routes import admin, environments, exec, files, health, processes, shells
from app.audit import AuditLog
from app.constants import AUDIT_FILE, SERVICE_NAME
from app.environments.quotas import Quotas, QuotaStore
from app.environments.service import EnvironmentService
from app.environments.store import EnvironmentStore
from app.errors import install_error_handlers
from app.files import FileService
from app.keyring.auth import TokenVerifier
from app.keyring.client import CredentialClient
from app.logging import configure_logging
from app.middleware import install_middleware
from app.preferences import build_preference_source
from app.sandbox import HostCapabilities, build_sandbox, probe_host
from app.settings import Settings, load_settings

if TYPE_CHECKING:
    import httpx
    from keyring_client import Clock

log = structlog.get_logger(__name__)


async def _reaper_loop(service: EnvironmentService, interval: float) -> None:
    while True:
        await asyncio.sleep(interval)
        try:
            report = await asyncio.to_thread(service.reap)
        except Exception:
            log.exception("reaper_failed")
            continue
        if report.shells_closed or report.environments_archived or report.logs_pruned:
            log.info(
                "reaper_pass",
                shells_closed=report.shells_closed,
                environments_archived=report.environments_archived,
                logs_pruned=report.logs_pruned,
            )


def create_app(
    settings: Settings | None = None,
    *,
    keyring_transport: httpx.AsyncBaseTransport | None = None,
    clock: Clock | None = None,
    capabilities: HostCapabilities | None = None,
    settings_client: Any = None,
) -> FastAPI:
    """Build the app. Tests pass their own settings, keyring transport, clock and host.

    ``settings_client`` is substituted by tests with a fake settings-api client.
    Constructed at startup; it makes no network call until the first resolve.
    """
    settings = settings or load_settings()
    configure_logging(settings.log_json, settings.log_level)
    preferences = build_preference_source(settings, client=settings_client)

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
        caps = capabilities or probe_host()
        sandbox = build_sandbox(caps, settings.min_tier)
        store = EnvironmentStore(settings.root)
        quotas = QuotaStore(settings.root, Quotas.from_settings(settings))
        audit = AuditLog(settings.root / AUDIT_FILE)
        service = EnvironmentService(settings, store, quotas, sandbox, audit)
        service.startup()
        # Neither keyring client touches the network here: the first token to arrive is what
        # fetches keyring's keys, so a keyring that is down cannot stop this service starting.
        keyring_clock: Clock = clock if clock is not None else SystemClock()
        keyring_log = structlog.get_logger("app.keyring")
        jwks = JwksClient(
            url=jwks_url(settings.keyring_base_url),
            clock=keyring_clock,
            cache_seconds=settings.jwks_cache_seconds,
            min_refetch_seconds=settings.jwks_min_refetch_seconds,
            timeout_seconds=settings.keyring_timeout_seconds,
            transport=keyring_transport,
            logger=keyring_log,
        )
        credentials = CredentialClient(
            SharedCredentialClient(
                base_url=settings.keyring_base_url,
                service_token=settings.keyring_service_token.get_secret_value(),
                timeout_seconds=settings.keyring_timeout_seconds,
                transport=keyring_transport,
                logger=keyring_log,
            )
        )
        app.state.settings = settings
        app.state.preferences = preferences
        app.state.service = service
        app.state.audit = audit
        app.state.jwks = jwks
        app.state.verifier = TokenVerifier(
            SharedTokenVerifier(
                jwks=jwks, issuer=settings.keyring_issuer, clock=keyring_clock, logger=keyring_log
            ),
            settings.keyring_service_name,
        )
        app.state.credentials = credentials
        app.state.files = FileService(settings.max_file_read_bytes, settings.max_file_write_bytes)
        reaper = asyncio.create_task(_reaper_loop(service, settings.reaper_interval_seconds))
        log.info("started", sandbox_tier=sandbox.tier.label, root=str(settings.root))
        try:
            yield
        finally:
            reaper.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await reaper
            await asyncio.to_thread(service.shutdown)
            await preferences.aclose()
            await credentials.aclose()
            await jwks.aclose()

    app = FastAPI(title=SERVICE_NAME, version="0.1.0", lifespan=lifespan)
    install_error_handlers(app)
    install_middleware(app)
    for router in (health, environments, shells, processes, files, exec, admin):
        app.include_router(router.router)
    return app


app = create_app()
