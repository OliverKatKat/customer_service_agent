"""Select and load task-specific instructions without granting new permissions."""

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from langchain_core.messages import HumanMessage

from app.agent.authorization import is_valid_refund_authorization
from app.agent.state import AgentState


REFUND_TOOL_NAMES = (
    "inspect_refund_request",
    "consult_company_knowledge",
    "process_large_refund",
)
_REFUND_TERMS = (
    "退款",
    "退钱",
    "退货",
    "取消订单",
    "取消这笔订单",
    "未到账",
    "没到账",
    "退回款",
    "refund",
    "chargeback",
)
_REFUND_SKILL_PATH = Path(__file__).parent / "refund" / "SKILL.md"


@dataclass(frozen=True)
class AgentSkill:
    name: str
    instructions: str
    allowed_tools: tuple[str, ...]


@lru_cache(maxsize=1)
def _load_refund_skill() -> AgentSkill:
    source = _REFUND_SKILL_PATH.read_text(encoding="utf-8")
    if not source.startswith("---\n"):
        raise ValueError(f"Missing YAML front matter: {_REFUND_SKILL_PATH}")
    _, separator, instructions = source[4:].partition("\n---\n")
    if not separator or not instructions.strip():
        raise ValueError(f"Missing skill instructions: {_REFUND_SKILL_PATH}")
    return AgentSkill(
        name="refund",
        instructions=instructions.strip(),
        allowed_tools=REFUND_TOOL_NAMES,
    )


def _latest_user_text(state: AgentState) -> str:
    for message in reversed(state.get("messages", ())):
        if isinstance(message, HumanMessage):
            return message.content if isinstance(message.content, str) else ""
    return ""


def select_skill(state: AgentState) -> AgentSkill | None:
    """Choose a workflow guide; authorization remains in backend tools."""
    context = state.get("context") or {}
    if is_valid_refund_authorization(
        context.get("authorization"),
        ticket_id=context.get("ticket_id"),
        tenant_id=context.get("tenant_id"),
    ):
        return _load_refund_skill()

    user_text = _latest_user_text(state).lower()
    if any(term in user_text for term in _REFUND_TERMS):
        return _load_refund_skill()
    return None


def allowed_tools_for_skill(skill_name: str | None) -> frozenset[str] | None:
    if skill_name == "refund":
        return frozenset(REFUND_TOOL_NAMES)
    return None
