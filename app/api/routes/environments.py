"""Environment CRUD, summary, usage and reset."""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, Query, Response

from app.api.deps import CallerDep, ServiceDep, SettingsDep
from app.api.schemas import CreateEnvironmentRequest
from app.constants import EnvironmentState

router = APIRouter(prefix="/v1/environments", tags=["environments"])


@router.post("", status_code=201)
async def create_environment(
    body: CreateEnvironmentRequest, caller: CallerDep, service: ServiceDep
) -> dict[str, Any]:
    """Create an environment under the caller's account and profile."""
    record = await asyncio.to_thread(
        service.create, caller, body.name, body.labels, body.credentials, body.network, body.limits
    )
    return service.environment_view(record)


@router.get("")
async def list_environments(
    caller: CallerDep,
    service: ServiceDep,
    profile: str | None = None,
    state: EnvironmentState | None = None,
    label: str | None = Query(default=None, description="key or key=value"),
) -> dict[str, Any]:
    """List the caller's environments."""
    records = service.list_environments(caller, profile=profile, state=state, label=label)
    return {"environments": [service.environment_view(r) for r in records]}


@router.get("/{environment_id}")
async def get_environment(
    environment_id: str, caller: CallerDep, service: ServiceDep
) -> dict[str, Any]:
    """One environment."""
    return service.environment_view(service.get(caller, environment_id))


@router.get("/{environment_id}/summary")
async def environment_summary(
    environment_id: str, caller: CallerDep, service: ServiceDep
) -> dict[str, Any]:
    """Environment, shells, processes and recent commands."""
    return await asyncio.to_thread(service.summary, caller, environment_id)


@router.get("/{environment_id}/usage")
async def environment_usage(
    environment_id: str, caller: CallerDep, service: ServiceDep
) -> dict[str, Any]:
    """Disk, shells and processes against the limits."""
    return await asyncio.to_thread(service.usage, caller, environment_id)


@router.delete("/{environment_id}", status_code=204)
async def delete_environment(
    environment_id: str, caller: CallerDep, service: ServiceDep, settings: SettingsDep
) -> Response:
    """Kill everything and remove the folder."""
    await asyncio.to_thread(service.delete, caller, environment_id)
    return Response(status_code=204)


@router.post("/{environment_id}/reset")
async def reset_environment(
    environment_id: str, caller: CallerDep, service: ServiceDep
) -> dict[str, Any]:
    """Wipe the workspace, keep the environment."""
    record = await asyncio.to_thread(service.reset, caller, environment_id)
    return service.environment_view(record)
