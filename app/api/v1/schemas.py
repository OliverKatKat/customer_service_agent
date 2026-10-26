# app/api/v1/schemas.py
from pydantic import BaseModel, Field, validator

class TicketCreate(BaseModel):
    topic: str = Field(..., min_length=1, max_length=100, description="工单的主题或用户的初次提问")
    # 客户身份只从服务端验证过的登录信息取得。
    order_id: str = Field(default="", description="相关的订单号（可选）")

    @validator('topic')
    def topic_must_not_be_sensitive(cls, v):
        if "反动" in v: 
            raise ValueError('包含敏感词汇')
        return v

class TaskResponse(BaseModel):
    trace_id: str
    status: str
    output: str


class TicketApprove(BaseModel):
    trace_id: str = Field(..., description="要放行的工单会话ID (即之前生成的 trace_id)")
    approval_code: str = Field(..., min_length=16, max_length=128, description="管理员后台二次审批凭据")

class ReviewApprove(BaseModel):
    expected_version: int = Field(..., ge=1)
    approval_code: str = Field(..., min_length=16, max_length=128)
    reason: str = Field(default="", max_length=500)


class ReviewReject(BaseModel):
    expected_version: int = Field(..., ge=1)
    approval_code: str = Field(..., min_length=16, max_length=128)
    reason: str = Field(..., min_length=3, max_length=500)