# app/api/v1/auth.py
from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import OAuth2PasswordRequestForm
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from app.core.db import get_db
from app.models.user import User
from app.models.tenant import Tenant
from app.core.security import create_access_token, verify_password

router = APIRouter()

@router.post("/login")
async def login(
    form_data: OAuth2PasswordRequestForm = Depends(),
    db: AsyncSession = Depends(get_db) # 👈 注入数据库 Session
):
    # 1. 去数据库捞这个用户
    result = await db.execute(select(User).where(User.username == form_data.username))
    user = result.scalars().first()
    
    # 2. 校验：用户不存在、密码错误、或者账号被封禁
    if not user or not verify_password(form_data.password, user.hashed_password):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="用户名或密码错误",
            headers={"WWW-Authenticate": "Bearer"},
        )
        
    if not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="该账号已被管理员封禁",
        )
    tenant = await db.get(Tenant, user.tenant_id)
    if tenant is None or not tenant.is_active:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="商家账号不可用")
    
    # 3. 校验通过！生成带有 role 的 JWT 门票
    access_token = create_access_token(data={"sub": user.username, "role": user.role, "tenant_id": user.tenant_id})
    
    return {
        "access_token": access_token, 
        "token_type": "bearer",
        "role": user.role, # 返回给前端，方便展示不同的 UI
        "tenant_id": user.tenant_id
    }