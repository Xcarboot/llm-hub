from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.core.config import settings


class ChatMessage(BaseModel):
    role: Literal["system", "user", "assistant", "tool"]
    content: str | list[dict[str, Any]]


class ChatCompletionRequest(BaseModel):
    model_config = ConfigDict(extra="allow")

    model: str = Field(min_length=1, max_length=100)
    messages: list[ChatMessage] = Field(min_length=1, max_length=200)
    max_tokens: int = Field(gt=0, le=settings.max_output_tokens)
    stream: bool = False
    temperature: float | None = Field(default=None, ge=0, le=2)

    @field_validator("messages")
    @classmethod
    def limit_message_characters(cls, value: list[ChatMessage]) -> list[ChatMessage]:
        total = 0
        for message in value:
            if isinstance(message.content, str):
                total += len(message.content)
            else:
                total += len(str(message.content))
        if total > settings.max_message_chars:
            raise ValueError(f"messages exceed {settings.max_message_chars} characters")
        return value


class ModelItem(BaseModel):
    id: str
    object: str = "model"
    owned_by: str = "gateway"


class ModelList(BaseModel):
    object: str = "list"
    data: list[ModelItem]
