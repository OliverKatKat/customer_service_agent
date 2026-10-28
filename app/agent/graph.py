# app/agent/graph.py
import os
import asyncio
from dotenv import load_dotenv
from psycopg_pool import AsyncConnectionPool
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.graph import StateGraph, START, END
from langgraph.prebuilt import tools_condition
from app.agent.state import AgentState
from app.agent.executor import executor_node
from app.agent.intent_guard import intent_guard_node
from app.agent.memory_gateway import memory_gateway_node
from app.agent.planner import planner_node
from app.core.config import settings

load_dotenv()

raw_url = settings.DATABASE_URL
pg_conn_str = raw_url.replace("postgresql+asyncpg://", "postgresql://")

# 🚨 1. 把直接实例化改成单例模式变量
_connection_pool = None

async def get_pool():
    global _connection_pool
    # 增加状态检查，防止复用已关闭的池
    if _connection_pool is None or _connection_pool.closed:
        _connection_pool = AsyncConnectionPool(
            conninfo=pg_conn_str,
            max_size=20,
            kwargs={"autocommit": True},
            open=False
        )
        await _connection_pool.open() 
    return _connection_pool

# 🚨 新增：真正的生产级优雅关闭钩子
async def close_pool():
    global _connection_pool
    if _connection_pool is not None and not _connection_pool.closed:
        await _connection_pool.close()
        _connection_pool = None

# ==========================================
# 构建闭环图模型 (这部分保持不变)
workflow = StateGraph(AgentState)

# 1. 注册4个节点
workflow.add_node("intent_guard", intent_guard_node)   # 👈 新增安检节点
workflow.add_node("memory_gateway", memory_gateway_node)
workflow.add_node("planner", planner_node)
workflow.add_node("tools", executor_node)

# 2. 🚨 定义安检路由逻辑
def route_intent(state: AgentState):
    decision = state.get("routing_decision")
    if decision is not None:
        if decision.get("outcome") == "allow":
            return "memory_gateway"
        # Unknown decisions fail closed at this security boundary.
        return END

    # Read compatibility for checkpoints created before routing_decision existed.
    if state.get("intent_blocked", False):
        return END
    return "memory_gateway"

# 2. 🚨 重构数据流向 (Edges)
workflow.add_edge(START, "intent_guard")                          # 进门先过安检防火墙
workflow.add_conditional_edges("intent_guard", route_intent)      # 防火墙决定：进监狱(END)还是放行
def route_gateway(state: AgentState):
    return END if state.get("gateway_blocked") else "planner"

workflow.add_conditional_edges("memory_gateway", route_gateway)

# 3. 大脑与手脚的循环判断
workflow.add_conditional_edges("planner", tools_condition)
workflow.add_edge("tools", "planner")

# 🚨 3. 修改获取 app 的工厂函数
async def get_agent_app():
    from app.core.vector_db import initialize_memory_collection
    await asyncio.to_thread(initialize_memory_collection)
    # 在这里动态获取连接池
    pool = await get_pool()
    memory = AsyncPostgresSaver(pool)
    
    # 🎯 修复点：强制 LangGraph 检查并初始化它自己的内部记忆表
    # 这条命令底层用的是 CREATE TABLE IF NOT EXISTS，所以每次运行都很安全
    await memory.setup() 
    
    return workflow.compile(checkpointer=memory)
