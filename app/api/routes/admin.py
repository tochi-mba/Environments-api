"""Operator-only routes: per-account quotas, every environment, the audit log."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Query

from app.api.deps import AuditDep, OperatorDep, ServiceDep
from app.api.schemas import QuotaOverridesRequest

router = APIRouter(prefix="/v1/admin", tags=["admin"])


@router.get("/quotas/{account_id}")
async def get_quotas(account_id: str, operator: OperatorDep, service: ServiceDep) -> dict[str, Any]:
    """Effective quotas for an account."""
    return service.quotas_for(account_id)


@router.put("/quotas/{account_id}")
async def set_quotas(
    account_id: str, body: QuotaOverridesRequest, operator: OperatorDep, service: ServiceDep
) -> dict[str, Any]:
    """Replace an account's overrides (an empty object clears them)."""
    return service.set_quotas(operator, account_id, body.overrides)


@router.get("/environments")
async def list_all_environments(operator: OperatorDep, service: ServiceDep) -> dict[str, Any]:
    """Every environment on this deployment."""
    return {"environments": [service.environment_view(r) for r in service.list_all()]}


@router.get("/audit")
async def audit_tail(
    operator: OperatorDep,
    audit: AuditDep,
    limit: int = Query(default=100, ge=1, le=1000),
    account_id: str | None = None,
) -> dict[str, Any]:
    """The most recent audit events."""
    return {"events": audit.tail(limit, account_id)}
