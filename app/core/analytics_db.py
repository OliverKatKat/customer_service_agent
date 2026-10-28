"""Fail-closed, read-only connection for fixed operational statistics."""

from functools import lru_cache

from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.config import settings

_created_engines = set()


@lru_cache(maxsize=2)
def analytics_engine(url: str):
    engine = create_async_engine(url, pool_pre_ping=True)
    _created_engines.add(engine)
    return engine


async def close_analytics_engines():
    for engine in list(_created_engines):
        await engine.dispose()
    _created_engines.clear()
    analytics_engine.cache_clear()


async def get_analytics_db():
    url = settings.ANALYTICS_READONLY_DATABASE_URL
    if not url:
        raise HTTPException(503, "未配置独立只读分析连接；人工处理仍可使用")
    try:
        if make_url(url).drivername != "postgresql+asyncpg":
            raise HTTPException(503, "分析连接需要 PostgreSQL asyncpg 驱动")
        factory = async_sessionmaker(analytics_engine(url), expire_on_commit=False)
        async with factory() as session:
            async with session.begin():
                await session.execute(
                    text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
                )
                await session.execute(text("SET LOCAL statement_timeout = '3000ms'"))
                unsafe = await session.scalar(
                    text("""
                    SELECT (SELECT rolsuper OR rolbypassrls FROM pg_roles WHERE rolname=current_user)
                       OR EXISTS (
                         SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
                         WHERE n.nspname='public' AND c.relkind IN ('r','p')
                         AND has_table_privilege(c.oid,'INSERT,UPDATE,DELETE,TRUNCATE')
                       )
                """)
                )
                if unsafe:
                    raise HTTPException(
                        503, "分析账号具有写权限或绕过权限，请配置专用只读账号"
                    )
                yield session
    except HTTPException:
        raise
    except (SQLAlchemyError, ValueError):
        raise HTTPException(503, "只读分析连接不可用；请核对配置与数据库授权") from None
