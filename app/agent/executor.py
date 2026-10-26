from langchain_core.messages import ToolMessage
from langchain_core.runnables import RunnableConfig

from app.agent.skills.registry import allowed_tools_for_skill
from app.agent.state import AgentState
from app.agent.tool_registry import AVAILABLE_TOOLS


async def executor_node(state: AgentState, config: RunnableConfig):
    last_message = state["messages"][-1]
    allowed_tools = allowed_tools_for_skill(state.get("selected_skill"))

    if hasattr(last_message, "tool_calls") and last_message.tool_calls:
        results = []
        for tool_call in last_message.tool_calls:
            tool_name = tool_call['name']
            tool_call_id = tool_call['id']

            if allowed_tools is not None and tool_name not in allowed_tools:
                results.append(
                    ToolMessage(
                        content=(
                            f"当前 Skill 不允许调用 {tool_name}。"
                            "请使用当前 Skill 允许的工具，或转人工处理。"
                        ),
                        tool_call_id=tool_call_id,
                    )
                )
                continue

            if tool_name in AVAILABLE_TOOLS:
                print(f"--- 🛠️ 正在执行工具: {tool_name} ---")
                tool_func = AVAILABLE_TOOLS[tool_name]

                try:
                    result = await tool_func.ainvoke(tool_call['args'], config=config)
                    results.append(ToolMessage(content=str(result), tool_call_id=tool_call_id))
                except Exception as e:
                    error_msg = f"工具 {tool_name} 执行时发生系统错误: {str(e)}"
                    print(f"🚨 [执行器报错] {error_msg}")
                    results.append(ToolMessage(content=error_msg, tool_call_id=tool_call_id))

            else:
                available_list = list(AVAILABLE_TOOLS.keys())
                error_msg = (
                    f"❌ 严重安全拦截：你尝试调用的工具 '{tool_name}' 不存在！\n"
                    f"【系统警告】请停止捏造工具。你只能从以下白名单中选择合适的工具进行调用：\n"
                    f"[{', '.join(available_list)}]\n"
                    f"如果以上工具都无法解决当前问题，请直接回复客户你无法处理，严禁虚构操作。"
                )
                print(f"🛑 [幻觉拦截] 抓到大模型胡编乱造工具: '{tool_name}'，已执行硬拦截并下发警告。")
                results.append(ToolMessage(content=error_msg, tool_call_id=tool_call_id))

        return {"messages": results}

    return {"messages": []}
