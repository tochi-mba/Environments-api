"""Retrieval without a shell."""

from __future__ import annotations

import asyncio
import dataclasses
from typing import Any

from fastapi import APIRouter, Query

from app.api.deps import CallerDep, FilesDep, ServiceDep
from app.api.schemas import WriteFileRequest

router = APIRouter(prefix="/v1/environments/{environment_id}/files", tags=["files"])


@router.get("")
async def list_directory(
    environment_id: str,
    caller: CallerDep,
    service: ServiceDep,
    files: FilesDep,
    path: str = Query(default="."),
) -> dict[str, Any]:
    """List a directory in the workspace."""
    workspace, _ = service.workspace_for(caller, environment_id)
    entries = await asyncio.to_thread(files.list_dir, workspace, path)
    return {
        "environment_id": environment_id,
        "path": path,
        "entries": [dataclasses.asdict(e) for e in entries],
    }


@router.get("/content")
async def read_file(
    environment_id: str,
    caller: CallerDep,
    service: ServiceDep,
    files: FilesDep,
    path: str = Query(min_length=1),
    offset: int = Query(default=0, ge=0),
    max_bytes: int | None = Query(default=None, ge=1),
) -> dict[str, Any]:
    """Read a file, capped; ``truncated`` says whether there is more."""
    workspace, _ = service.workspace_for(caller, environment_id)
    content = await asyncio.to_thread(files.read, workspace, path, offset, max_bytes)
    return {"environment_id": environment_id, **dataclasses.asdict(content)}


@router.put("/content")
async def write_file(
    environment_id: str,
    body: WriteFileRequest,
    caller: CallerDep,
    service: ServiceDep,
    files: FilesDep,
) -> dict[str, Any]:
    """Write a file, creating parent directories."""
    workspace, owner = service.workspace_for(caller, environment_id)
    size = await asyncio.to_thread(
        files.write, workspace, body.path, body.content, body.encoding, body.mode, owner
    )
    service.note_write(caller, environment_id, body.path, size)
    return {"environment_id": environment_id, "path": body.path, "size": size}
