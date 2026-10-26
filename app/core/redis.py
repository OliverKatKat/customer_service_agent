import redis.asyncio as redis
from app.core.config import settings

# 1. 创建全局异步连接池
# decode_responses=True 是个神仙参数，它会自动把 Redis 返回的 bytes 转换成 Python 字符串
redis_pool = redis.ConnectionPool.from_url(
    settings.REDIS_URL,
    decode_responses=True,
    max_connections=10 # 限制最大连接数，防止搞炸 Redis
)

# 2. 实例化全局 Redis 客户端
redis_client = redis.Redis(connection_pool=redis_pool)

# 3. 优雅关闭的钩子（等会儿我们可以挂载到 main.py 的 lifespan 里）
async def close_redis_connection():
    await redis_client.aclose()