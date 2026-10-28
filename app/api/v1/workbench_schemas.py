from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

CaseType = Literal["refund", "return", "complaint", "consultation", "other"]
CaseStatus = Literal["open", "in_progress", "resolved", "closed"]
ReasonCode = Literal["product_fault", "delivery_delay", "changed_mind", "other"]


class NoteCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    content: str = Field(min_length=1, max_length=4000)


class CasePatch(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    expected_version: int = Field(ge=1)
    case_type: CaseType | None = None
    status: CaseStatus | None = None
    reason_code: ReasonCode | None = None
    reason: str = Field(default="", max_length=500)

    @model_validator(mode="after")
    def actual_change(self):
        changed = self.model_fields_set & {"case_type", "status", "reason_code"}
        if not changed:
            raise ValueError("至少提供一个案件字段")
        if any(
            getattr(self, field) is None for field in changed & {"case_type", "status"}
        ):
            raise ValueError("案件类型和状态不能为 null")
        if self.status in {"resolved", "closed"} and len(self.reason) < 3:
            raise ValueError("结束案件需要至少三个字符的处理说明")
        return self


class WorkItemPatch(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    expected_version: int = Field(ge=1)
    status: Literal["open", "done"] | None = None
    assigned_to: str | None = Field(default=None, min_length=1, max_length=100)
    due_at: datetime | None = None

    @field_validator("due_at")
    @classmethod
    def timezone_required(cls, value):
        if value is not None and (value.tzinfo is None or value.utcoffset() is None):
            raise ValueError("处理目标时间必须带时区")
        return value

    @model_validator(mode="after")
    def actual_change(self):
        changed = self.model_fields_set - {"expected_version"}
        if not changed:
            raise ValueError("至少提供一个待办字段")
        if any(
            getattr(self, field) is None for field in changed & {"status", "due_at"}
        ):
            raise ValueError("状态和目标时间不能为 null")
        return self
