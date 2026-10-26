import asyncio
import random

from langchain_core.tools import tool
from pydantic import Field
from sqlalchemy import desc, select
from sqlalchemy.orm.exc import StaleDataError

from app.agent.tool_runtime import tool_wrapper
from app.core.cache import get_session, invalidate_cache, set_session
from app.core.db import AsyncSessionLocal
from app.models.task import Task


@tool_wrapper(timeout=8, max_retries=2)
async def _core_create_task(title: str, content: str = ""):
    print(f"--- 🛠️ [底层逻辑] 准备向数据库插入新任务: {title} ---")
    
    async with AsyncSessionLocal() as db:
        new_task = Task(title=title, content=content, status="pending")
        db.add(new_task)
        await db.commit() 
        
        # 🎯 修复 1：大模型默认爱查 5 条，我们就把 limit_5 的缓存炸掉！
        # (如果你的缓存工具没有加 session: 前缀，这里也不要加)
        await invalidate_cache("task_list_limit_5")
        
        return f"任务 '{title}' 创建成功！"

# ==========================================
# 2. 外层包装：专门给 LLM 用的标准化工具
# ==========================================
@tool
async def create_task(title: str = Field(..., description="新任务的标题")):
    """当你需要向数据库中创建一个全新的任务时，调用此工具。"""
    print(f"--- 🤖 [AI 调度层] LLM 请求调用 create_task, title={title} ---")
    return await _core_create_task(title=title)



# ==========================================
# 1. 内层核心逻辑：穿上防弹衣，加上极速缓存！
# ==========================================
@tool_wrapper(timeout=8, max_retries=2)
async def _core_fetch_task_list(limit: int):
    print(f"--- 🛠️ [底层逻辑] 准备获取前 {limit} 条记录 ---")
    
    cache_key = f"task_list_limit_{limit}"
    
    cached_data = await get_session(cache_key)
    if cached_data:
        return cached_data["tasks"]
        
    print("🐢 [Cache Miss] Redis 中没找到，准备查询底层 PostgreSQL...")
    
    async with AsyncSessionLocal() as db:
        # 🎯 修复 2：加上 order_by(desc(Task.id))，强制拿最新创建的！
        result = await db.execute(
            select(Task).order_by(desc(Task.id)).limit(limit)
        )
        tasks = result.scalars().all()
        
        if not tasks:
            return "数据库中目前没有任务。"
            
        task_list = [{"id": t.id, "title": t.title, "status": t.status} for t in tasks]
        
        #随机TTL防雪崩
        base_ttl = 60
        #0% - 12%随机抖动
        jitter = random.randint(0,int(base_ttl * 0.2))
        final_ttl = base_ttl + jitter

        await set_session(session_id=cache_key, data={"tasks": task_list}, expire_seconds=final_ttl)
        
        return task_list

# ==========================================
# 2. 外层包装：专门给 LLM 用的标准化工具 (保持不变)
# ==========================================
@tool
async def fetch_task_list(limit: int = Field(default=5, description="需要获取的任务数量限制")):
    """当你需要查询数据库中最近生成的任务列表时，调用此工具。它会返回任务的标题和当前状态。"""
    print(f"--- 🤖 [AI 调度层] LLM 请求调用 fetch_task_list, 参数 limit={limit} ---")
    return await _core_fetch_task_list(limit=limit)


#=================================================================
# 封装一个新的专门用于修改状态的内部函数
#=================================================================
async def _core_update_task_status(task_id: int, new_status: str):
    max_retries = 3
    
    for attempt in range(1, max_retries + 1):
        try:
            async with AsyncSessionLocal() as db:
                # 1. 读出当前数据（附带了 version）
                result = await db.execute(select(Task).where(Task.id == task_id))
                task = result.scalars().first()
                
                if not task:
                    return f"找不到 ID 为 {task_id} 的任务。"

                # 2. 修改状态
                task.status = new_status
                
                # 3. 提交修改
                # 💡 此时 SQLAlchemy 在底层偷偷执行的是：
                # UPDATE tasks SET status='xxx', version=version+1 WHERE id=? AND version=?
                await db.commit() 
                
                return f"✅ 任务 {task_id} 已成功更新为 {new_status} (当前版本: {task.version})"
                
        # 🎯 核心防御：捕获并发修改导致的冲突异常
        except StaleDataError:
            print(f"⚠️ [竞态防护] 第 {attempt} 次更新失败，任务被其他请求抢先修改！准备重试...")
            if attempt < max_retries:
                # 随机避让一下，防止多个冲突请求再次在同一时刻撞车
                await asyncio.sleep(0.1 * attempt)
                continue # 继续下一轮循环，重新去数据库读最新的 version
            else:
                return f"❌ 任务更新失败：当前系统并发修改太高，已重试 {max_retries} 次。"

# 暴露给大模型的工具
@tool
async def update_task_status(task_id: int, new_status: str):
    """当需要更新任务状态时调用此工具。"""
    return await _core_update_task_status(task_id, new_status)


# ==========================================
