"""The one-call shape: open an ephemeral shell, run, return, close."""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter

from app.api.deps import CallerDep, CredentialsDep, ServiceDep
from app.api.routes.shells import command_result, resolve_credentials
from app.api.schemas import ExecOnceRequest

router = APIRouter(prefix="/v1", tags=["exec"])


@router.post("/exec")
async def exec_once(
    body: ExecOnceRequest, caller: CallerDep, service: ServiceDep, credentials: CredentialsDep
) -> dict[str, Any]:
    """Run one command in a fresh shell and close it afterwards, whatever happened."""
    record = service.get(caller, body.environment_id)
    resolved, missing = await resolve_credentials(record, caller, credentials)
    shell = await asyncio.to_thread(
        service.open_shell, caller, body.environment_id, body.cwd, body.env, body.pty
    )
    try:
        # A subshell keeps `exit N` from taking the ephemeral shell down with it, so the
        # exit code is reported the way a caller of sh -c would expect.
        command = await asyncio.to_thread(
            service.exec, caller, shell.id, f"( {body.command}\n)", body.timeout_ms, resolved
        )
        await asyncio.to_thread(shell.wait_command, command, body.timeout_ms / 1000 + 5)
        result = command_result(shell, command, body.max_output_bytes)
    finally:
        await asyncio.to_thread(service.close_shell, caller, shell.id)
    # The subshell is this route's detail, not the caller's command; the audit log keeps
    # the wrapped form because that is what the shell ran.
    result["command"] = body.command
    result["shell_state"] = shell.state.value
    result["credentials_injected"] = [c.service for c in resolved]
    result["credentials_missing"] = missing
    return result
