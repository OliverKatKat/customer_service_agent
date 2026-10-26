from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from sqlalchemy.orm import DeclarativeBase
from app.core.config import settings

# 创建异步引擎
engine = create_async_engine(settings.DATABASE_URL, echo=False)

# 创建异步会话工厂
#配置对话需求，这里的expire_on_commit = False是为了防止提交数据后为了保持同步而自己去抓取数据
AsyncSessionLocal = async_sessionmaker(bind=engine, expire_on_commit=False)

# 定义所有模型的基类
class Base(DeclarativeBase):
    pass

# 这就是清单里的依赖注入 get_db()
async def get_db():
    async with AsyncSessionLocal() as session:
        yield session