from __future__ import annotations

from typing import Protocol

from csbox.api.models import ApiRequest, ApiResponse


class ApiTransport(Protocol):
    """Port used by API runners to execute an already-resolved request."""

    async def send(self, request: ApiRequest) -> ApiResponse:
        """Send one request and return only safe response metadata and text."""


__all__ = ["ApiTransport"]
