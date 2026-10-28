"""Shared identity dependencies. Importing them does not load the Agent/worker."""

from fastapi import Depends, HTTPException
from fastapi.security import OAuth2PasswordBearer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.core.security import verify_token
from app.models.tenant import Tenant
from app.models.user import User

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/v1/auth/login")


async def get_current_user(
    token: str = Depends(oauth2_scheme), db: AsyncSession = Depends(get_db)
):
    payload = verify_token(token)
    if not payload:
        raise HTTPException(
            401, "门票无效或已过期", headers={"WWW-Authenticate": "Bearer"}
        )
    tenant_id, username = payload.get("tenant_id"), payload.get("sub")
    if (
        not isinstance(tenant_id, str)
        or not tenant_id.strip()
        or not isinstance(username, str)
        or not username.strip()
    ):
        raise HTTPException(401, "身份信息不完整，请重新登录")
    user = (
        await db.execute(
            select(User).where(User.tenant_id == tenant_id, User.username == username)
        )
    ).scalar_one_or_none()
    if user is None or not user.is_active:
        raise HTTPException(401, "账号不可用，请重新登录")
    tenant = await db.get(Tenant, tenant_id)
    if tenant is None or not tenant.is_active:
        raise HTTPException(401, "商家账号不可用")
    # Read the role from the database, never from the unrefreshed token claim.
    identity = {"sub": user.username, "role": user.role, "tenant_id": user.tenant_id}
    await db.commit()  # Do not hold an identity-read transaction during SSE.
    return identity


async def verify_admin_role(current_user: dict = Depends(get_current_user)):
    if current_user["role"] != "admin":
        raise HTTPException(403, "权限不足：只有高级客服/管理员才能执行此高危操作！")
    return current_user


async def require_staff(current_user: dict = Depends(get_current_user)):
    if current_user["role"] not in {"admin", "staff"}:
        raise HTTPException(403, "需要售后工作人员权限")
    return current_user


async def require_analyst(current_user: dict = Depends(get_current_user)):
    if current_user["role"] not in {"admin", "analyst"}:
        raise HTTPException(403, "需要经营分析权限")
    return current_user
