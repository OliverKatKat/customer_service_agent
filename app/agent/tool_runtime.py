import asyncio
import logging
import time
from functools import wraps


logger = logging.getLogger("agent.tools")


def tool_wrapper(timeout: int = 8, max_retries: int = 2):
    def decorator(func):
        @wraps(func)
        async def wrapper(*args, **kwargs):
            tool_name = func.name if hasattr(func, 'name') else func.__name__
            last_error = None
            
            for attempt in range(1, max_retries + 2):
                attempt_start = time.perf_counter()
                try:
                    result = await asyncio.wait_for(func(*args, **kwargs), timeout=timeout)
                    latency = round(time.perf_counter() - attempt_start, 3)
                    logger.info(f"tool_name={tool_name}, attempt={attempt}, latency={latency}s, status=success")
                    return result
                    
                except (asyncio.TimeoutError, Exception) as e:
                    latency = round(time.perf_counter() - attempt_start, 3)
                    error_type = "Timeout" if isinstance(e, asyncio.TimeoutError) else type(e).__name__
                    last_error = str(e) or error_type
                    
                    # 🚨 强制打印到控制台，无视 logging 级别屏蔽
                    print(f"--- ⚠️ [预警] 工具 {tool_name} 第 {attempt} 次失败 | 耗时: {latency}s | 原因: {error_type} ---")
                    
                    logger.warning(
                        f"tool_name={tool_name}, attempt={attempt}, "
                        f"latency={latency}s, status=error, error={error_type}"
                    )
                    
                    if attempt <= max_retries:
                        wait_time = min((2 ** attempt) * 0.5, 10)
                        print(f"--- ⏳ [等待] {wait_time} 秒后发起重试... ---")
                        await asyncio.sleep(wait_time)
                        
            fallback_msg = f"工具 [{tool_name}] 经过 {max_retries} 次重试后失败。最后一次错误: {last_error}"
            print(f"--- ❌ [彻底失败] 已拦截崩溃，返回降级信息: {fallback_msg} ---")
            return fallback_msg
            
        return wrapper
    return decorator
