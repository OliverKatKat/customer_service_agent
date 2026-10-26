from app.agent.skills.registry import REFUND_TOOL_NAMES
from app.agent.tools import (
    search_user_memory,
    process_large_refund,
)
from app.agent.refund_tools import inspect_refund_request
from app.agent.knowledge_agent import consult_company_knowledge
from langchain_openai import ChatOpenAI
from app.core.config import settings


# Centralize model construction and the tool allowlist used by planning/execution.
llm = ChatOpenAI(
    model=settings.LLM_MODEL_NAME,
    api_key=settings.DEEPSEEK_API_KEY,
    base_url=settings.LLM_BASE_URL,
    temperature=settings.LLM_TEMPERATURE,
    extra_body={"thinking": {"type": "disabled"}},
)

llm_with_tools = llm.bind_tools([
    consult_company_knowledge,
    search_user_memory,
    process_large_refund,
    inspect_refund_request,
])

AVAILABLE_TOOLS = {
    "consult_company_knowledge": consult_company_knowledge,
    "search_user_memory": search_user_memory,
    "process_large_refund": process_large_refund,
    "inspect_refund_request": inspect_refund_request,
}

refund_llm_with_tools = llm.bind_tools(
    [AVAILABLE_TOOLS[name] for name in REFUND_TOOL_NAMES]
)
