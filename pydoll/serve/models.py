"""Pydantic request/response models for pydoll-serve."""

from __future__ import annotations

from typing import Any, Literal, Optional

from pydantic import BaseModel, Field


class SessionOptions(BaseModel):
    """Options for creating a browser session."""

    headless: bool = True
    proxy: Optional[str] = None
    geolocation: Optional[str] = None
    extra_args: list[str] = Field(default_factory=list)


class CreateSessionRequest(BaseModel):
    """Request body for POST /sessions."""

    options: SessionOptions = Field(default_factory=SessionOptions)


class CreateSessionResponse(BaseModel):
    """Response body for POST /sessions."""

    session_id: str


class NavigateRequest(BaseModel):
    """Request body for POST /sessions/{id}/navigate."""

    url: str
    timeout: int = Field(default=30000, description='Timeout in milliseconds')
    wait_until: Literal['load', 'domcontentloaded', 'networkidle'] = 'load'


class Action(BaseModel):
    """A single browser action."""

    type: Literal['click', 'type', 'scroll', 'wait', 'evaluate', 'keypress', 'sleep']
    selector: Optional[str] = None
    text: Optional[str] = None
    key: Optional[str] = None
    script: Optional[str] = None
    # for wait actions: 'selector' | 'sleep'
    wait_for: Optional[str] = Field(default=None, alias='for')
    value: Optional[str] = None
    ms: Optional[int] = None
    x: Optional[int] = None
    y: Optional[int] = None

    model_config = {'populate_by_name': True}


class ActionsRequest(BaseModel):
    """Request body for POST /sessions/{id}/actions."""

    actions: list[Action]
    continue_on_error: bool = False


class ActionResult(BaseModel):
    """Result for a single action."""

    index: int
    type: str
    success: bool
    result: Optional[Any] = None
    error: Optional[str] = None


class ActionsResponse(BaseModel):
    """Response body for POST /sessions/{id}/actions."""

    results: list[ActionResult]


class ContentResponse(BaseModel):
    """Response body for GET /sessions/{id}/content."""

    content: str


class ExtractionField(BaseModel):
    """A field to extract from the page."""

    name: str
    selector: str
    attribute: Optional[str] = None


class ExtractionModel(BaseModel):
    """Model for structured content extraction."""

    fields: list[ExtractionField]


class ExtractRequest(BaseModel):
    """Request body for POST /sessions/{id}/extract."""

    model: ExtractionModel


class CookieParam(BaseModel):
    """A cookie to set."""

    name: str
    value: str
    domain: Optional[str] = None
    path: Optional[str] = None
    secure: Optional[bool] = None
    http_only: Optional[bool] = None
    expires: Optional[float] = None


class SetCookiesRequest(BaseModel):
    """Request body for POST /sessions/{id}/cookies."""

    cookies: list[CookieParam]


class ErrorResponse(BaseModel):
    """Standard error response."""

    error: str
    type: str
    session_id: Optional[str] = None
