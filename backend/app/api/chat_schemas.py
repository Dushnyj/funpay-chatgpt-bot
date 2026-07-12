from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator


class _FromAttributes(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class ChatSummaryOut(_FromAttributes):
    id: int
    funpay_chat_id: str
    buyer_funpay_id: str | None = None
    funpay_order_id: str | None = None
    order_id: int | None = None
    unread_count: int
    last_message_text: str | None = None
    last_message_direction: str | None = None
    last_message_at: datetime | None = None


class ChatMessageOut(_FromAttributes):
    id: int
    conversation_id: int
    funpay_message_id: str | None = None
    direction: str
    sender_funpay_id: str | None = None
    text: str
    delivery_status: str
    is_read: bool
    created_at: datetime


class SendChatMessageRequest(BaseModel):
    text: str = Field(min_length=1, max_length=4000)

    @field_validator("text")
    @classmethod
    def validate_text(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("Message must not be blank")
        return stripped


class MarkChatReadResponse(BaseModel):
    status: str
    unread_count: int
