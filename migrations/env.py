import asyncio
from logging.config import fileConfig
from sqlalchemy import pool
from sqlalchemy.ext.asyncio import async_engine_from_config
from alembic import context

# 1. 导入你的 Base 和 Settings
from app.core.db import Base
from app.core.config import settings
import app.models  # register all application tables with Base.metadata

config = context.config

# 2. 这里的 target_metadata 必须指向你的模型
# 这样 --autogenerate 才知道要建哪些表
target_metadata = Base.metadata 
def include_name(name, type_, parent_names):
    # 告诉 Alembic：碰到这三张表，当做没看见，绝对不要去动它们！
    if type_ == "table" and name in ["checkpoints", "checkpoint_blobs", "checkpoint_writes", "checkpoint_migrations"]:
        return False
    return True
    
def run_migrations_offline() -> None:
    """离线模式"""
    url = settings.DATABASE_URL
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        include_name=include_name # 👈 2. 加入离线模式配置
    )
    with context.begin_transaction():
        context.run_migrations()

def do_run_migrations(connection):
    context.configure(
        connection=connection, 
        target_metadata=target_metadata,
        include_name=include_name # 👈 3. 加入在线同步执行配置
    )
    with context.begin_transaction():
        context.run_migrations()

async def run_migrations_online() -> None:
    """在线模式：异步适配"""
    # 动态获取配置，并将 sqlalchemy.url 设置为你的 .env 地址
    configuration = config.get_section(config.config_ini_section)
    configuration["sqlalchemy.url"] = settings.DATABASE_URL
    
    connectable = async_engine_from_config(
        configuration,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)

    await connectable.dispose()

if context.is_offline_mode():
    run_migrations_offline()
else:
    # 异步运行
    asyncio.run(run_migrations_online())