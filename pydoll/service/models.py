from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel, Field


class CreateSessionRequest(BaseModel):
    options: dict[str, Any] = Field(default_factory=dict)


class NavigateRequest(BaseModel):
    url: str
    timeout: Optional[int] = None
    wait_until: Optional[str] = 'load'


class ActionRequest(BaseModel):
    type: str
    selector: Optional[str] = None
    text: Optional[str] = None
    key: Optional[str] = None
    script: Optional[str] = None
    for_: Optional[str] = Field(default=None, alias='for')
    value: Optional[str] = None
    ms: Optional[int] = None
    timeout: Optional[int] = None


class ActionsBody(BaseModel):
    actions: list[ActionRequest]
    continue_on_error: bool = False
    timeout: Optional[int] = None


class ExtractFieldRequest(BaseModel):
    name: str
    selector: str
    attribute: Optional[str] = None


class ExtractModelRequest(BaseModel):
    fields: list[ExtractFieldRequest]


class ExtractBody(BaseModel):
    model: ExtractModelRequest
