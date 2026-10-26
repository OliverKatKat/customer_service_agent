# app/core/security.py
from datetime import datetime, timedelta
from typing import Optional
from jose import JWTError, jwt
import bcrypt  # 👈 直接引入原生 bcrypt
from app.core.config import settings

def verify_password(plain_password: str, hashed_password: str) -> bool:
    """校验明文密码与数据库哈希值是否匹配"""
    # bcrypt 要求输入必须是 bytes 类型，所以先 encode
    return bcrypt.checkpw(
        plain_password.encode("utf-8"), 
        hashed_password.encode("utf-8")
    )

def get_password_hash(password: str) -> str:
    """将明文密码加密为哈希值"""
    # 自动生成盐值并进行哈希
    hashed = bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt())
    # 存入数据库前，将其转回标准的字符串
    return hashed.decode("utf-8")

# --- 下方保留你原有的 JWT 逻辑 ---
def create_access_token(data: dict, expires_delta: Optional[timedelta] = None):
    to_encode = data.copy()
    if expires_delta:
        expire = datetime.utcnow() + expires_delta
    else:
        expire = datetime.utcnow() + timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES)
    
    to_encode.update({"exp": expire})
    encoded_jwt = jwt.encode(to_encode, settings.SECRET_KEY, algorithm=settings.ALGORITHM)
    return encoded_jwt

def verify_token(token: str):
    try:
        payload = jwt.decode(token, settings.SECRET_KEY, algorithms=[settings.ALGORITHM])
        return payload
    except JWTError:
        return None