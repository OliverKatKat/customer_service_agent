from pathlib import Path
import os
import logging
from contextlib import asynccontextmanager
from fastapi import FastAPI, Response, status
from fastapi.responses import FileResponse
from redis.asyncio import Redis
from app.core.config import settings
from app.core.logging import JSONFormatter
from app.core.tracing import instrument_fastapi, setup_tracing
from app.api.v1.auth import router as auth_router
from app.api.v1.endpoints import router as api_router
from app.agent.graph import get_pool
from prometheus_fastapi_instrumentator import Instrumentator
from app.core.config import settings

# 1. 定义日志配置函数
def setup_logging():
    logger = logging.getLogger("agent")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()

    # 控制台输出
    ch = logging.StreamHandler()
    ch.setFormatter(JSONFormatter())
    logger.addHandler(ch)

    # 文件输出
    log_dir = "logs"
    if not os.path.exists(log_dir):
        os.makedirs(log_dir)
        
    fh = logging.FileHandler(f"{log_dir}/agent.log", encoding='utf-8')
    fh.setFormatter(JSONFormatter()) 
    logger.addHandler(fh)

    logger.propagate = False

# 🌟 关键修复：在这里真正调用函数，激活你的日志系统！
setup_tracing()
setup_logging() 

# 2. 定义生命周期 (Lifespan)
@asynccontextmanager
async def lifespan(app: FastAPI):
    # 真正开启连接池。此时 FastAPI 已经准备好了 Event Loop，所以不会报错
    pool = await get_pool()
    
    yield # 把控制权交还给 FastAPI，服务器开始正常运行处理请求
    
    # 🌟 服务器关闭时执行：
    print("🛑 正在优雅关闭记忆库连接...")
    await pool.close() # 释放数据库连接

# 3. 创建唯一且全局的 FastAPI 实例
app = FastAPI(
    title=settings.PROJECT_NAME,
    lifespan=lifespan
)
instrument_fastapi(app)
# ==========================================
# 🚨 核心挂载：Prometheus 监控指标暴露
# ==========================================
# instrument() 会拦截并记录所有的请求耗时(Histogram)和请求次数(Counter)
# expose() 会自动在 /metrics 路径上暴露出标准的文本格式指标
Instrumentator(
    should_group_status_codes=False, # 区分 200, 202, 401, 429 等具体状态码
    should_ignore_untemplated=True,  # 忽略未定义的路由（防恶意扫描污染指标）
).instrument(app).expose(app, endpoint="/metrics")
# ==========================================

# 4. 挂载所有的路由 (Routers)
app.include_router(auth_router, prefix="/api/v1/auth", tags=["Authentication"])
app.include_router(api_router, prefix="/api/v1", tags=["Agent"])

@app.get("/review", include_in_schema=False)
def review_workspace():
    """Browser demo for customer status and tenant-scoped admin review."""
    return FileResponse(Path(__file__).resolve().parent / "web" / "review.html")


@app.get("/chat", include_in_schema=False)
def chat_demo():
    """Customer chat demo streaming agent progress events."""
    return FileResponse(Path(__file__).resolve().parent / "web" / "chat.html")


@app.get("/demo", include_in_schema=False)
def demo_landing():
    """Demo landing page linking the chat and review workspaces."""
    return FileResponse(Path(__file__).resolve().parent / "web" / "demo.html")
# 5. 定义根路径接口
@app.get("/", tags=["Root"])
def read_root():
    return {"message": "看到这个说明你成功了！", "project_name": settings.PROJECT_NAME}

# 🚨 升级版：深度健康检查 (Deep Healthcheck)
# ==========================================
@app.get("/healthz", tags=["Root"])
async def health_check(response: Response):
    """
    检查所有强依赖组件的连通性。如果任何一个组件失联，返回 503 触发 K8s 容器重启。
    """
    status_report = {
        "status": "ok",
        "api": "healthy",
        "database": "unknown",
        "redis": "unknown"
    }
    
    # 1. 探测 PostgreSQL 连通性
    try:
        pool = await get_pool()
        async with pool.connection() as conn:
            # 执行一个极轻量的查询
            await conn.execute("SELECT 1")
            status_report["database"] = "healthy"
    except Exception as e:
        status_report["database"] = "unhealthy"
        status_report["status"] = "error"
        status_report["db_error_detail"] = str(e)
        logger.error(f"🔴 [健康检查] 数据库连接失败: {str(e)}")

    # 2. 探测 Redis 连通性
    try:
        redis_client = Redis.from_url(settings.REDIS_URL, socket_connect_timeout=2)
        # PING 命令测试连通性
        await redis_client.ping()
        status_report["redis"] = "healthy"
    except Exception as e:
        status_report["redis"] = "unhealthy"
        status_report["status"] = "error"
        status_report["redis_error_detail"] = str(e)
        logger.error(f"🔴 [健康检查] Redis 连接失败: {str(e)}")
        
    finally:
        # 关闭探测专用的 Redis 连接
        if 'redis_client' in locals():
            await redis_client.aclose()

    # 3. 裁决最终状态
    if status_report["status"] != "ok":
        # 只要有组件不健康，立刻改变 HTTP 状态码为 503
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        
    return status_report
