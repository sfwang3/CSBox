from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, JsonValue, PrivateAttr, field_validator

if TYPE_CHECKING:
    from csbox.api.redaction import Redactor

ApiMethod = Literal["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"]
ApiAssertionStatus = Literal["PASS", "FAIL", "SKIP"]
ApiRunStatus = Literal["PASS", "FAIL", "CONFIG_ERROR", "RUNTIME_ERROR"]


class _ApiModel(BaseModel):
    """Base for public API domain objects with a deliberately closed schema."""

    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)


class ApiMultipartPart(_ApiModel):
    """A static multipart field accepted by the first API request model."""

    name: str
    value: str


class ApiRequest(_ApiModel):
    method: ApiMethod
    url: str
    headers: dict[str, str] = Field(default_factory=dict)
    query: dict[str, str] = Field(default_factory=dict)
    json_body: JsonValue = None
    body: str | None = None
    form: dict[str, str] = Field(default_factory=dict)
    multipart: tuple[ApiMultipartPart, ...] = ()
    timeout_seconds: float | None = Field(default=None, gt=0)
    follow_redirects: bool = False
    verify_tls: bool = True


class ApiResponse(_ApiModel):
    """A transient response; external consumers must use :meth:`redacted_copy`."""

    _raw_assertion_view: ApiResponse | None = PrivateAttr(default=None)

    status_code: int = Field(ge=0, le=999)
    headers: dict[str, str] = Field(default_factory=dict)
    body: str = ""
    url: str
    elapsed_ms: float = Field(default=0.0, ge=0.0)
    truncated: bool = False
    content_type: str | None = None
    response_size: int = Field(
        default=0,
        ge=0,
        description="Original response byte size when exact, otherwise captured lower bound.",
    )
    response_size_exact: bool = Field(
        default=True,
        description="False when response_size is only the captured lower bound.",
    )

    def _attach_assertion_view(self, response: ApiResponse) -> None:
        """Attach a raw in-memory response for internal assertion evaluation only."""

        self._raw_assertion_view = response

    def assertion_view(self) -> ApiResponse:
        """Return transient raw assertion data when a transport supplied it."""

        return self._raw_assertion_view or self

    def __copy__(self) -> Self:
        """Copy public fields without extending the raw assertion lifetime."""

        copied = super().__copy__()
        copied._raw_assertion_view = None
        return copied

    def __deepcopy__(self, memo: dict[int, Any] | None = None) -> Self:
        """Deep-copy public fields without extending the raw assertion lifetime."""

        copied = super().__deepcopy__(memo)
        copied._raw_assertion_view = None
        return copied

    def __getstate__(self) -> dict[Any, Any]:
        """Never pickle the transient raw assertion view."""

        state = super().__getstate__()
        private = dict(state["__pydantic_private__"] or {})
        private["_raw_assertion_view"] = None
        state["__pydantic_private__"] = private
        return state

    def redacted_copy(self, redactor: Redactor) -> ApiResponse:
        """Return the only view permitted for persistence, evidence, logs, and TUI."""

        return ApiResponse(
            status_code=self.status_code,
            headers=redactor.headers(self.headers),
            body=redactor.response_body(self.body, self.content_type),
            url=redactor.url(self.url),
            elapsed_ms=self.elapsed_ms,
            truncated=self.truncated,
            content_type=(None if self.content_type is None else redactor.text(self.content_type)),
            response_size=self.response_size,
            response_size_exact=self.response_size_exact,
        )


class ApiAssertion(_ApiModel):
    kind: str
    expected: JsonValue = None
    location: str | None = None
    operator: str | None = None


class ApiAssertionResult(_ApiModel):
    assertion: ApiAssertion
    status: ApiAssertionStatus
    message: str
    actual: JsonValue = None


class ApiStep(_ApiModel):
    name: str
    request: ApiRequest
    assertions: tuple[ApiAssertion, ...] = ()


class ApiScenario(_ApiModel):
    name: str
    steps: tuple[ApiStep, ...]
    variables: dict[str, str] = Field(default_factory=dict)
    source: str | None = None


class ApiRunResult(_ApiModel):
    step_name: str
    status: ApiRunStatus = "PASS"
    response: ApiResponse | None = None
    assertions: tuple[ApiAssertionResult, ...] = ()
    error: str | None = None
    elapsed_ms: float = Field(default=0.0, ge=0.0)

    @field_validator("response")
    @classmethod
    def _detach_assertion_view(cls, response: ApiResponse | None) -> ApiResponse | None:
        return None if response is None else response.model_copy(deep=True)


class ApiRun(_ApiModel):
    id: str
    scenario: ApiScenario
    started_at: datetime
    status: ApiRunStatus = "PASS"
    results: tuple[ApiRunResult, ...] = ()
    ended_at: datetime | None = None
    elapsed_ms: float = Field(default=0.0, ge=0.0)


class ApiEvidence(_ApiModel):
    title: str
    request: ApiRequest
    response: ApiResponse | None = None
    result: ApiRunResult | None = None

    @field_validator("response")
    @classmethod
    def _detach_assertion_view(cls, response: ApiResponse | None) -> ApiResponse | None:
        return None if response is None else response.model_copy(deep=True)


__all__ = [
    "ApiAssertion",
    "ApiAssertionResult",
    "ApiAssertionStatus",
    "ApiEvidence",
    "ApiMethod",
    "ApiMultipartPart",
    "ApiRequest",
    "ApiResponse",
    "ApiRun",
    "ApiRunResult",
    "ApiRunStatus",
    "ApiScenario",
    "ApiStep",
]
