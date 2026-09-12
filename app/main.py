"""Application factory and process lifecycle."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator

import httpx
import structlog
from fastapi import FastAPI

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
from app.keyring.jwks import JWKSCache
from app.logging import configure_logging
from app.middleware import install_middleware
from app.sandbox import HostCapabilities, build_sandbox, probe_host
from app.settings import Settings

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
    http_client: httpx.AsyncClient | None = None,
    capabilities: HostCapabilities | None = None,
) -> FastAPI:
    """Build the app. Tests pass their own settings, HTTP client and host capabilities."""
    settings = settings or Settings()
    configure_logging(settings.log_json, settings.log_level)

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        caps = capabilities or probe_host()
        sandbox = build_sandbox(caps, settings.min_tier)
        store = EnvironmentStore(settings.root)
        quotas = QuotaStore(settings.root, Quotas.from_settings(settings))
        audit = AuditLog(settings.root / AUDIT_FILE)
        service = EnvironmentService(settings, store, quotas, sandbox, audit)
        service.startup()
        client = http_client or httpx.AsyncClient(timeout=settings.keyring_timeout_seconds)
        jwks = JWKSCache(client, settings.jwks_url, settings.jwks_cache_seconds)
        app.state.settings = settings
        app.state.service = service
        app.state.audit = audit
        app.state.jwks = jwks
        app.state.verifier = TokenVerifier(jwks, settings.keyring_service_name)
        app.state.credentials = CredentialClient(
            client, settings.keyring_base_url, settings.keyring_service_token
        )
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
            if http_client is None:
                await client.aclose()

    app = FastAPI(title=SERVICE_NAME, version="0.1.0", lifespan=lifespan)
    install_error_handlers(app)
    install_middleware(app)
    for router in (health, environments, shells, processes, files, exec, admin):
        app.include_router(router.router)
    return app


app = create_app()
