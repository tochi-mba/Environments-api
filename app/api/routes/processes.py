"""Live PIDs and signalling, guarded by ownership."""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter

from app.api.deps import CallerDep, ServiceDep
from app.api.schemas import SignalRequest

router = APIRouter(prefix="/v1", tags=["processes"])


@router.get("/environments/{environment_id}/processes")
async def list_environment_processes(
    environment_id: str, caller: CallerDep, service: ServiceDep
) -> dict[str, Any]:
    """Every live process in the environment."""
    found = await asyncio.to_thread(service.list_processes, caller, environment_id)
    return {"environment_id": environment_id, "processes": [p.to_dict() for p in found]}


@router.get("/shells/{shell_id}/processes")
async def list_shell_processes(
    shell_id: str, caller: CallerDep, service: ServiceDep
) -> dict[str, Any]:
    """Just that shell's tree."""
    found = await asyncio.to_thread(service.list_shell_processes, caller, shell_id)
    return {"shell_id": shell_id, "processes": [p.to_dict() for p in found]}


@router.post("/environments/{environment_id}/processes/{pid}/signal")
async def signal_process(
    environment_id: str, pid: int, body: SignalRequest, caller: CallerDep, service: ServiceDep
) -> dict[str, Any]:
    """Signal one process, provided it belongs to this environment."""
    sig = body.number()
    owner = await asyncio.to_thread(service.signal_process, caller, environment_id, pid, sig)
    return {"environment_id": environment_id, "pid": pid, "signal": sig, "shell_id": owner}
