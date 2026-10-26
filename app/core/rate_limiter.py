# app/core/rate_limiter.py
import time
import uuid
from redis.asyncio import Redis

# 🚨 核心：用 Lua 脚本保证绝对的原子性！
# 返回值：[是否放行(1=是, 0=否), Retry-After秒数]
LUA_SLIDING_WINDOW = """
local key = KEYS[1]
local limit = tonumber(ARGV[1])
local window = tonumber(ARGV[2])
local now = tonumber(ARGV[3])
local member = ARGV[4]

-- 1. 移除滑动窗口之外的过期请求记录 (时间戳 < now - window)
local window_start = now - window
redis.call('ZREMRANGEBYSCORE', key, '-inf', window_start)

-- 2. 获取当前窗口内的请求总数
local current_count = redis.call('ZCARD', key)

if current_count >= limit then
    -- 3a. 触发限流！计算 Retry-After
    -- 获取窗口内最早的那条请求时间，算算它还有多久过期
    local oldest = redis.call('ZRANGE', key, 0, 0, 'WITHSCORES')
    local retry_after = window
    if oldest and oldest[2] then
        retry_after = window - (now - tonumber(oldest[2]))
    end
    -- 返回 0 表示拒绝，并附带需要等待的秒数
    return {0, math.ceil(retry_after)}
else
    -- 3b. 允许放行！将当前请求加入 ZSET，并刷新过期时间兜底
    redis.call('ZADD', key, now, member)
    redis.call('EXPIRE', key, window)
    return {1, 0}
end
"""

class RedisRateLimiter:
    def __init__(self, redis_client: Redis):
        self.redis = redis_client
        # 在 Redis 实例上注册这段 Lua 脚本，提升后续执行效率
        self.script = self.redis.register_script(LUA_SLIDING_WINDOW)

    async def is_allowed(self, user_id: str, limit: int, window: int) -> tuple[bool, int]:
        """
        判断是否放行。
        返回 (is_allowed, retry_after)
        """
        key = f"rate_limit:sliding_window:{user_id}"
        now = time.time()
        # 用 uuid 作为 member，保证每次请求在 ZSET 里的唯一性
        member = str(uuid.uuid4())
        
        # 执行 Lua 脚本 (KEYS[1], ARGV[1], ARGV[2], ARGV[3], ARGV[4])
        result = await self.script(
            keys=[key], 
            args=[limit, window, now, member]
        )
        
        # 解析 Lua 返回的结果: {1/0, retry_after}
        is_allowed = bool(result[0])
        retry_after = int(result[1])
        
        return is_allowed, retry_after