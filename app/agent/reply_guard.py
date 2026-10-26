"""Small, deterministic checks for customer-facing action claims."""

import re


_REVIEW_SUBMITTED = re.compile(
    r"(?:已|已经)(?:为您)?(?:提交|转交|发起).{0,10}(?:人工)?审核"
    r"|(?:人工)?审核.{0,10}(?:已|已经)提交"
)
_HUMAN_HANDOFF = re.compile(
    r"(?:我|我们|系统).{0,10}(?:会|将|已|可以|帮您).{0,16}"
    r"(?:转交|转接|转人工|升级|登记)"
    r"|(?:我会|我将|我帮您).{0,20}(?:记录|登记).{0,8}工单"
    r"|需要为您.{0,6}(?:转交|转人工)"
)


_REFUND_COMPLETION_PATTERNS = (
    re.compile(r"(?:退款(?:申请)?|申请).{0,16}(?:处理好了?|处理完毕|处理完成|办好了?|办完了?|完成了|办结|退款成功)"),
    re.compile(r"(?:我|我们|系统)?(?:已|已经)(?:为您)?处理完毕"),
    re.compile(r"(?:已|已经)(?:为您)?(?:处理好了?|处理完毕|处理完成|办好了?|办完了?|完成了).{0,10}(?:退款|退款申请)"),
    re.compile(r"(?:退款|款项).{0,8}(?:已到账|已打款|退款成功)"),
)
_AMBIGUOUS_PENDING_REFUND_PHRASES = (
    re.compile(r"(?:您的)?退款(?:申请)?(?:我|我们)?(?:已经|已)?(?:为您)?(?:处理好了?|处理完毕|处理完成|办好了?|办完了?|完成了|办结|退款成功)"),
    re.compile(r"(?:我|我们|系统)?(?:已|已经)(?:为您)?处理完毕"),
    re.compile(r"(?:退款|款项).{0,8}(?:已到账|已打款|退款成功)"),
)
_PENDING_REVIEW_DISCLOSURE = re.compile(
    r"(?:提交|转交|进入|申请|正在|等待|待|尚在).{0,24}(?:人工)?(?:审核|审批)"
    r"|(?:人工)?(?:审核|审批).{0,16}(?:中|等待|待处理|批准前|通过前|尚未|结果)"
)
_PENDING_REFUND_SENTENCE = "退款申请已提交人工审核，目前仍在等待审核，尚未退款。"


def claims_refund_completion(message: str) -> bool:
    """Detect wording that could imply a refund is complete or fully handled."""
    return any(pattern.search(message) for pattern in _REFUND_COMPLETION_PATTERNS)


def discloses_pending_review(message: str) -> bool:
    """Whether the reply clearly says review is pending or in progress."""
    return bool(_PENDING_REVIEW_DISCLOSURE.search(message))


def ground_pending_refund_reply(message: str) -> str:
    """Replace ambiguous completion wording while preserving the rest of the reply."""
    for pattern in _AMBIGUOUS_PENDING_REFUND_PHRASES:
        if pattern.search(message):
            grounded = pattern.sub(_PENDING_REFUND_SENTENCE, message, count=1)
            return re.sub(r"。[ \t]*[。:：,，；;]+[ \t]*", "。", grounded, count=1)
    return message


def claims_review_submission(message: str) -> bool:
    """Whether a reply claims that a refund review has already been submitted."""
    return bool(_REVIEW_SUBMITTED.search(message))


def claims_human_handoff(message: str) -> bool:
    """Whether a reply promises or reports a handoff that should have a record."""
    return bool(_HUMAN_HANDOFF.search(message))
