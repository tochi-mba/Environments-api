"""Retrieval without a shell."""

from __future__ import annotations

import asyncio
import dataclasses
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Header, Query, Response

from app.api.deps import CallerDep, FilesDep, ServiceDep
from app.api.schemas import (
    DirectoryRequest,
    EditFileRequest,
    PatchFileRequest,
    TransferFileRequest,
    WriteFileRequest,
)
from app.file_search import search

router = APIRouter(prefix="/v1/environments/{environment_id}/files", tags=["files"])


@router.get("")
async def list_directory(
    environment_id: str,
    caller: CallerDep,
    service: ServiceDep,
    files: FilesDep,
    path: str = Query(default="."),
    glob: str | None = Query(default=None),
    depth: int = Query(default=0, ge=0, le=10),
) -> dict[str, Any]:
    """List a directory in the workspace."""
    workspace, _ = service.workspace_for(caller, environment_id)
    entries = await asyncio.to_thread(files.list_dir, workspace, path, glob, depth)
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
    response: Response,
    path: str = Query(min_length=1),
    offset: int = Query(default=0, ge=0),
    max_bytes: int | None = Query(default=None, ge=1),
) -> dict[str, Any]:
    """Read a file, capped; ``truncated`` says whether there is more."""
    workspace, _ = service.workspace_for(caller, environment_id)
    content = await asyncio.to_thread(files.read, workspace, path, offset, max_bytes)
    response.headers["ETag"] = content.etag
    return {"environment_id": environment_id, **dataclasses.asdict(content)}


@router.get("/search")
async def search_files(
    environment_id: str,
    caller: CallerDep,
    service: ServiceDep,
    files: FilesDep,
    pattern: str = Query(min_length=1),
    path: str = Query(default="."),
    glob: str | None = Query(default=None),
    depth: int = Query(default=10, ge=0, le=10),
    mode: Literal["files_with_matches", "content"] = Query(default="content"),
    limit: int = Query(default=100, ge=1, le=1000),
    before: int = Query(default=0, ge=0, le=20),
    after: int = Query(default=0, ge=0, le=20),
    max_file_bytes: int = Query(default=1024 * 1024, ge=1, le=16 * 1024 * 1024),
) -> dict[str, Any]:
    """Search bounded UTF-8 files literally, reporting every skipped class and limit."""
    workspace, _ = service.workspace_for(caller, environment_id)
    result = await asyncio.to_thread(
        search,
        files,
        workspace,
        path,
        pattern,
        glob,
        depth,
        mode,
        limit,
        before,
        after,
        max_file_bytes,
    )
    return {"environment_id": environment_id, **dataclasses.asdict(result)}


@router.put("/content")
async def write_file(
    environment_id: str,
    body: WriteFileRequest,
    caller: CallerDep,
    service: ServiceDep,
    files: FilesDep,
    response: Response,
    if_match: Annotated[str | None, Header(alias="If-Match")] = None,
) -> dict[str, Any]:
    """Write a file, creating parent directories."""
    workspace, owner = service.workspace_for(caller, environment_id)
    result = await asyncio.to_thread(
        files.write_result,
        workspace,
        body.path,
        body.content,
        body.encoding,
        body.mode,
        owner,
        if_match,
    )
    service.note_write(caller, environment_id, result.path, result.size)
    response.headers["ETag"] = result.etag
    return {"environment_id": environment_id, **dataclasses.asdict(result)}


@router.post("/edit")
async def edit_file(
    environment_id: str,
    body: EditFileRequest,
    caller: CallerDep,
    service: ServiceDep,
    files: FilesDep,
    response: Response,
    if_match: Annotated[str | None, Header(alias="If-Match")] = None,
) -> dict[str, Any]:
    """Replace one exact occurrence and return a reviewable diff."""
    workspace, owner = service.workspace_for(caller, environment_id)
    result = await asyncio.to_thread(
        files.edit, workspace, body.path, body.old_string, body.new_string, owner, if_match
    )
    service.note_write(caller, environment_id, result.path, result.size)
    response.headers["ETag"] = result.etag
    return {"environment_id": environment_id, **dataclasses.asdict(result)}


@router.post("/patch")
async def patch_file(
    environment_id: str,
    body: PatchFileRequest,
    caller: CallerDep,
    service: ServiceDep,
    files: FilesDep,
    response: Response,
    if_match: Annotated[str | None, Header(alias="If-Match")] = None,
) -> dict[str, Any]:
    """Apply a validated single-file unified patch and identify rejected hunks."""
    workspace, owner = service.workspace_for(caller, environment_id)
    result = await asyncio.to_thread(files.patch, workspace, body.path, body.patch, owner, if_match)
    service.note_write(caller, environment_id, result.path, result.size)
    response.headers["ETag"] = result.etag
    return {"environment_id": environment_id, **dataclasses.asdict(result)}


@router.post("/directories")
async def create_directory(
    environment_id: str,
    body: DirectoryRequest,
    caller: CallerDep,
    service: ServiceDep,
    files: FilesDep,
) -> dict[str, str]:
    """Create a directory and missing parents without following links."""
    workspace, owner = service.workspace_for(caller, environment_id)
    path = await asyncio.to_thread(files.mkdir, workspace, body.path, owner)
    return {"environment_id": environment_id, "path": path}


@router.delete("/content")
async def delete_file(
    environment_id: str,
    caller: CallerDep,
    service: ServiceDep,
    files: FilesDep,
    path: str = Query(min_length=1),
    recursive: bool = Query(default=False),
    if_match: Annotated[str | None, Header(alias="If-Match")] = None,
) -> dict[str, str]:
    """Delete one path; recursive directory deletion must be explicitly requested."""
    workspace, _ = service.workspace_for(caller, environment_id)
    deleted = await asyncio.to_thread(files.delete, workspace, path, recursive, if_match)
    return {"environment_id": environment_id, "path": deleted}


async def _transfer(
    environment_id: str,
    body: TransferFileRequest,
    caller: CallerDep,
    service: ServiceDep,
    files: FilesDep,
    response: Response,
    *,
    move: bool,
    if_match: str | None,
) -> dict[str, Any]:
    workspace, owner = service.workspace_for(caller, environment_id)
    result = await asyncio.to_thread(
        files.transfer,
        workspace,
        body.source,
        body.destination,
        move,
        owner,
        if_match,
    )
    service.note_write(caller, environment_id, result.path, result.size)
    response.headers["ETag"] = result.etag
    return {"environment_id": environment_id, **dataclasses.asdict(result)}


@router.post("/copy")
async def copy_file(
    environment_id: str,
    body: TransferFileRequest,
    caller: CallerDep,
    service: ServiceDep,
    files: FilesDep,
    response: Response,
    if_match: Annotated[str | None, Header(alias="If-Match")] = None,
) -> dict[str, Any]:
    """Copy one regular file without overwriting the destination."""
    return await _transfer(
        environment_id, body, caller, service, files, response, move=False, if_match=if_match
    )


@router.post("/move")
async def move_file(
    environment_id: str,
    body: TransferFileRequest,
    caller: CallerDep,
    service: ServiceDep,
    files: FilesDep,
    response: Response,
    if_match: Annotated[str | None, Header(alias="If-Match")] = None,
) -> dict[str, Any]:
    """Move one regular file without overwriting the destination."""
    return await _transfer(
        environment_id, body, caller, service, files, response, move=True, if_match=if_match
    )
