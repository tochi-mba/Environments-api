"""Shells and the commands run in them."""

from __future__ import annotations

import asyncio
import base64
from typing import Any

from fastapi import APIRouter, Query

from app.api.deps import CallerDep, CredentialsDep, ServiceDep, SettingsDep
from app.api.schemas import ExecRequest, OpenShellRequest, SignalRequest, StdinRequest, WaitRequest
from app.environments.models import EnvironmentRecord
from app.errors import ValidationError
from app.keyring.auth import Caller
from app.keyring.client import CredentialClient, ResolvedCredential
from app.shells.shell import CommandRecord, Shell

router = APIRouter(prefix="/v1", tags=["shells"])


async def resolve_credentials(
    record: EnvironmentRecord, caller: Caller, credentials: CredentialClient
) -> tuple[list[ResolvedCredential], list[str]]:
    """Resolve every credential the environment declares; missing ones are reported, not fatal."""
    resolved: list[ResolvedCredential] = []
    missing: list[str] = []
    for service in record.credentials:
        credential = await credentials.resolve(caller.user_token, record.profile, service)
        if credential is None:
            missing.append(service)
        else:
            resolved.append(credential)
    return resolved, missing


def command_result(
    shell: Shell, record: CommandRecord, max_bytes: int, *, tail: bool = False
) -> dict[str, Any]:
    """A command with the output it produced so far: its first ``max_bytes``, or its last."""
    end = record.output_end if record.output_end is not None else shell.cursor
    chunk = shell.read_span(record.output_start, end, max_bytes, tail=tail)
    return {
        **record.to_dict(),
        "output": chunk.data.decode("utf-8", "replace"),
        "output_dropped_bytes": chunk.dropped_bytes,
        "output_truncated_bytes": chunk.truncated_bytes,
        "output_cursor": chunk.next_cursor,
        "shell_state": shell.state.value,
    }


@router.post("/environments/{environment_id}/shells", status_code=201)
async def open_shell(
    environment_id: str, body: OpenShellRequest, caller: CallerDep, service: ServiceDep
) -> dict[str, Any]:
    """Open a shell in the environment."""
    shell = await asyncio.to_thread(
        service.open_shell, caller, environment_id, body.cwd, body.env, body.pty
    )
    return shell.to_dict()


@router.get("/environments/{environment_id}/shells")
async def list_shells(
    environment_id: str, caller: CallerDep, service: ServiceDep
) -> dict[str, Any]:
    """Every shell of the environment known to this process."""
    return {"shells": [s.to_dict() for s in service.list_shells(caller, environment_id)]}


@router.get("/shells/{shell_id}")
async def get_shell(shell_id: str, caller: CallerDep, service: ServiceDep) -> dict[str, Any]:
    """State, pid, pgid, current command and cursor."""
    return service.get_shell(caller, shell_id).to_dict()


@router.delete("/shells/{shell_id}")
async def close_shell(shell_id: str, caller: CallerDep, service: ServiceDep) -> dict[str, Any]:
    """SIGTERM the process group; SIGKILL after the grace period."""
    shell = await asyncio.to_thread(service.close_shell, caller, shell_id)
    return shell.to_dict()


@router.post("/shells/{shell_id}/exec")
async def exec_command(
    shell_id: str,
    body: ExecRequest,
    caller: CallerDep,
    service: ServiceDep,
    credentials: CredentialsDep,
    settings: SettingsDep,
) -> dict[str, Any]:
    """Run a command. With ``wait_ms`` the result comes back in the same call when it fits."""
    shell = service.get_shell(caller, shell_id)
    record = service.get(caller, shell.environment_id)
    resolved, missing = await resolve_credentials(record, caller, credentials)
    command = await asyncio.to_thread(
        service.exec, caller, shell_id, body.command, body.timeout_ms, resolved
    )
    if body.wait_ms:
        await asyncio.to_thread(shell.wait_command, command, body.wait_ms / 1000)
    result = command_result(shell, command, settings.max_file_read_bytes)
    result["credentials_injected"] = [c.service for c in resolved]
    result["credentials_missing"] = missing
    return result


@router.get("/shells/{shell_id}/output")
async def read_output(
    shell_id: str,
    caller: CallerDep,
    service: ServiceDep,
    settings: SettingsDep,
    cursor: int = Query(default=0, ge=0),
    max_bytes: int = Query(default=65536, ge=1),
    wait_ms: int = Query(default=0, ge=0, le=60_000),
) -> dict[str, Any]:
    """Output from ``cursor``; ``dropped_bytes`` is reported when the cursor fell behind."""
    shell = service.get_shell(caller, shell_id)
    if wait_ms:
        await asyncio.to_thread(shell.wait_output, cursor, wait_ms / 1000)
    chunk = shell.read_output(cursor, min(max_bytes, settings.max_file_read_bytes))
    current = shell.current
    return {
        "shell_id": shell.id,
        "shell_state": shell.state.value,
        "current_command_id": current.id if current else None,
        "cursor": chunk.cursor,
        "next_cursor": chunk.next_cursor,
        "end": chunk.end,
        "dropped_bytes": chunk.dropped_bytes,
        "data": chunk.data.decode("utf-8", "replace"),
        "data_base64": base64.b64encode(chunk.data).decode("ascii"),
    }


@router.post("/shells/{shell_id}/wait")
async def wait_shell(
    shell_id: str, body: WaitRequest, caller: CallerDep, service: ServiceDep
) -> dict[str, Any]:
    """Block until the shell is idle or ``timeout_ms`` passes."""
    shell = service.get_shell(caller, shell_id)
    idle = await asyncio.to_thread(shell.wait_idle, body.timeout_ms / 1000)
    last = shell.commands[-1].to_dict() if shell.commands else None
    return {
        "status": "idle" if idle else "timed_out",
        "shell": shell.to_dict(),
        "last_command": last,
    }


@router.post("/shells/{shell_id}/signal")
async def signal_shell(
    shell_id: str, body: SignalRequest, caller: CallerDep, service: ServiceDep
) -> dict[str, Any]:
    """Signal the current command's processes."""
    sig = body.number()
    count = await asyncio.to_thread(service.signal_shell, caller, shell_id, sig)
    return {"shell_id": shell_id, "signal": sig, "processes_signalled": count}


@router.post("/shells/{shell_id}/stdin")
async def write_stdin(
    shell_id: str, body: StdinRequest, caller: CallerDep, service: ServiceDep
) -> dict[str, Any]:
    """Write to the shell's stdin (or its tty)."""
    if body.encoding == "base64":
        try:
            data = base64.b64decode(body.data, validate=True)
        except ValueError as exc:
            raise ValidationError("data is not valid base64") from exc
    else:
        data = body.data.encode("utf-8")
    written = await asyncio.to_thread(service.write_stdin, caller, shell_id, data, body.target)
    return {"shell_id": shell_id, "bytes_written": written}


@router.get("/shells/{shell_id}/commands")
async def list_commands(shell_id: str, caller: CallerDep, service: ServiceDep) -> dict[str, Any]:
    """This process's history for the shell, oldest first."""
    shell = service.get_shell(caller, shell_id)
    return {"shell_id": shell.id, "commands": [c.to_dict() for c in shell.commands]}


@router.get("/commands/{command_id}")
async def get_command(
    command_id: str,
    caller: CallerDep,
    service: ServiceDep,
    settings: SettingsDep,
    offset: int = Query(default=0, ge=0),
    max_bytes: int = Query(default=1024 * 1024, ge=1),
) -> dict[str, Any]:
    """One command with its output read back from the log, live or after a restart."""
    view = await asyncio.to_thread(
        service.get_command,
        caller,
        command_id,
        offset,
        min(max_bytes, settings.max_file_read_bytes),
    )
    return {
        **view.command,
        "output": view.output.decode("utf-8", "replace"),
        "output_base64": base64.b64encode(view.output).decode("ascii"),
        "output_offset": view.output_offset,
        "output_truncated": view.output_truncated,
    }
