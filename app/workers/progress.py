from langchain_core.callbacks import AsyncCallbackHandler

from app.core.task_events import publish_task_event


_NODE_PROGRESS = {
    "intent_guard": ("checking_intent", "正在检查请求意图和安全边界"),
    "memory_gateway": ("preparing_context", "正在整理会话上下文"),
    "planner": ("planning", "正在分析问题并制定处理步骤"),
    "tools": ("using_tools", "正在查询知识库或业务工具"),
}


class AgentProgressCallback(AsyncCallbackHandler):
    """Expose coarse graph/tool progress without publishing prompts or tool data."""

    def __init__(self, redis_client, task_id: str):
        self.redis_client = redis_client
        self.task_id = task_id
        self._last_status = None

    async def _publish(self, status: str, message: str) -> None:
        if status == self._last_status:
            return
        self._last_status = status
        await publish_task_event(self.redis_client, self.task_id, status, message)

    async def on_chain_start(self, serialized, inputs, **kwargs):
        metadata = kwargs.get("metadata") or {}
        node_name = metadata.get("langgraph_node") or (serialized or {}).get("name")
        progress = _NODE_PROGRESS.get(node_name)
        if progress:
            await self._publish(*progress)

    async def on_tool_start(self, serialized, input_str, **kwargs):
        await self._publish("tool_running", "正在执行查询或处理步骤")

    async def on_tool_end(self, output, **kwargs):
        await self._publish("tool_step_completed", "查询步骤已返回，正在整理结果")

    async def on_tool_error(self, error, **kwargs):
        await self._publish("tool_recovering", "一个查询步骤未成功，Agent 正在检查是否可以恢复")
