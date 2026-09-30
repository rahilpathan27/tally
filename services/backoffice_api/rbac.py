"""Role-based access control: one permission matrix, deny by default.

Every back-office route declares exactly one permission through ``require``; the authorization
matrix test enumerates the app's routes and fails if any route lacks one.
"""

from __future__ import annotations

from collections.abc import Callable, Coroutine
from typing import Annotated, Any

from fastapi import Depends, HTTPException, Request

from services.backoffice_api.auth import Principal, current_principal

MERCHANT_ROLES = frozenset({"merchant_admin", "merchant_developer", "merchant_viewer"})
PLATFORM_ROLES = frozenset({"ops_analyst", "risk_analyst", "approver", "operator"})

PERMISSIONS: dict[str, frozenset[str]] = {
    # Merchant dashboard (tenant-scoped by RLS).
    "merchant:payments:read": frozenset(MERCHANT_ROLES),
    "merchant:refunds:create": frozenset({"merchant_admin", "merchant_developer"}),
    "merchant:settlements:read": frozenset({"merchant_admin", "merchant_viewer"}),
    "merchant:disputes:write": frozenset({"merchant_admin"}),
    "merchant:keys:manage": frozenset({"merchant_admin", "merchant_developer"}),
    "merchant:webhooks:manage": frozenset({"merchant_admin", "merchant_developer"}),
    "merchant:analytics:read": frozenset(MERCHANT_ROLES),
    # Operations and risk consoles (cross-tenant, audited).
    "ops:recon:read": frozenset({"ops_analyst", "approver", "operator"}),
    "ops:recon:act": frozenset({"ops_analyst"}),
    "ops:ledger:read": frozenset({"ops_analyst", "approver", "operator"}),
    "ops:ledger:adjust": frozenset({"ops_analyst"}),
    "ops:switch:read": frozenset({"ops_analyst", "operator", "risk_analyst"}),
    "ops:payments:read": frozenset({"ops_analyst", "risk_analyst", "approver", "operator"}),
    "ops:refunds:act": frozenset({"ops_analyst"}),
    "risk:reviews:act": frozenset({"risk_analyst"}),
    "risk:read": frozenset({"risk_analyst", "approver", "operator"}),
    "risk:rules:propose": frozenset({"risk_analyst"}),
    "risk:models:propose": frozenset({"risk_analyst", "operator"}),
    "aml:read": frozenset({"ops_analyst", "risk_analyst", "approver"}),
    "aml:act": frozenset({"ops_analyst", "risk_analyst"}),
    "limits:propose": frozenset({"operator"}),
    "approvals:read": frozenset({"approver", "ops_analyst", "risk_analyst", "operator"}),
    "approvals:decide": frozenset({"approver"}),
    "audit:read": frozenset({"approver", "operator"}),
    "chaos:control": frozenset({"operator"}),
}

PERMISSION_ATTRIBUTE = "__tally_permission__"


def allowed(principal: Principal, permission: str) -> bool:
    roles = PERMISSIONS.get(permission)
    return roles is not None and bool(roles & principal.roles)


def require(permission: str) -> Callable[..., Coroutine[Any, Any, Principal]]:
    if permission not in PERMISSIONS:
        raise ValueError(f"unknown permission {permission}")

    async def dependency(
        request: Request, principal: Annotated[Principal, Depends(current_principal)]
    ) -> Principal:
        if not allowed(principal, permission):
            raise HTTPException(
                403, detail={"code": "FORBIDDEN", "message": "Your role cannot do this."}
            )
        if permission.startswith("merchant:") and principal.merchant_id is None:
            raise HTTPException(403, detail={"code": "FORBIDDEN", "message": "Merchant only."})
        request.state.permission = permission
        return principal

    setattr(dependency, PERMISSION_ATTRIBUTE, permission)
    return dependency
