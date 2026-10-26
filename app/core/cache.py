import json
from typing import Optional, Any
from app.core.redis import redis_client

# 定义统一的缓存前缀，防止 Key 冲突
SESSION_PREFIX = "session:"

async def get_session(session_id: str) -> Optional[dict]:
    """
    Cache-Aside 读操作：先查缓存，查到直接返回。
    """
    cache_key = f"{SESSION_PREFIX}{session_id}"
    
    # 1. 尝试从 Redis 读取
    cached_data = await redis_client.get(cache_key)
    
    if cached_data:
        print(f"⚡ [Cache Hit] 命中缓存: {cache_key}")
        # 因为存进去的是 JSON 字符串，取出来要反序列化为字典
        return json.loads(cached_data)
        
    print(f"🐢 [Cache Miss] 缓存未命中: {cache_key}")
    return None

async def set_session(session_id: str, data: dict, expire_seconds: int = 3600):
    """
    Cache-Aside 写操作：更新数据到缓存，并设置过期时间 (TTL)
    """
    cache_key = f"{SESSION_PREFIX}{session_id}"
    
    # 将字典转为 JSON 字符串
    json_data = json.dumps(data, ensure_ascii=False)
    
    # setex = Set + Expire，原子操作写入并设置过期时间
    await redis_client.setex(name=cache_key, time=expire_seconds, value=json_data)
    print(f"💾 [Cache Set] 会话已缓存: {cache_key}, 过期时间: {expire_seconds}s")

async def invalidate_cache(cache_key: str):
    """
    主动删除缓存，确保数据一致性
    """
    await redis_client.delete(cache_key)
    print(f"🗑️ [Cache Invalidate] 脏缓存已清理，Key: {cache_key}")