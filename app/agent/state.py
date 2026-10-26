from typing import Annotated, Literal, NotRequired, Sequence, TypedDict
from langchain_core.messages import BaseMessage
from langgraph.graph.message import add_messages

def limit_messages(left:list, right:list) -> list:
    full_list = add_messages(left,right)
    return full_list[-10:] #只保留最后十条消息


class AgentAuthorization(TypedDict):
    source: Literal["admin_ticket_approval"]
    operation: Literal["large_refund"]
    actor_id: str
    tenant_id: str
    ticket_id: str
    order_id: str
    amount: float


class AgentContext(TypedDict, total=False):
    request_id: str
    tenant_id: str
    ticket_id: str
    conversation_id: str
    memory_scope_id: str
    customer_id: str
    authorization: AgentAuthorization


RoutingOutcome = Literal["allow", "block"]
RoutingReasonCode = Literal[
    "admin_approval_context",
    "supported_support_intent",
    "unsupported_or_malicious_intent",
    "classifier_output_invalid",
]


class RoutingDecision(TypedDict):
    """Stable safety-routing contract shared by graph nodes and later agents."""

    outcome: RoutingOutcome
    reason_code: RoutingReasonCode
    policy_version: str


class AgentState(TypedDict):
    # add_messages 会确保新消息是“追加”而不是“覆盖”，这形成了 Agent 的短期记忆
    messages: Annotated[Sequence[BaseMessage], add_messages]

    # Node outputs are partial state updates, so context and decisions are optional.
    context: NotRequired[AgentContext]
    routing_decision: NotRequired[RoutingDecision]
    gateway_blocked: NotRequired[bool]
    gateway_block_reason: NotRequired[str | None]
    selected_skill: NotRequired[str | None]

    # Compatibility for checkpoints written before routing_decision existed.
    intent_blocked: NotRequired[bool]
