"""Common adapter interface."""

from __future__ import annotations

from typing import Any

from app.llm.common import StreamProgress, error_text, is_status_error, status_code_of
from app.llm.endpoints import fixed_http_client
from app.llm.types import LLMResponse, StreamCallback, Target


def sdk_endpoint_kwargs(target: Target, default_base_url: str) -> dict[str, Any]:
    """``base_url`` (and, in manual mode, ``http_client``) for the SDK constructor.

    * auto mode: the SDK appends its own path to ``target.sdk_base_url``;
    * manual mode: the SDK still builds a request, but a fixed-URL transport
      rewrites it to the complete ``request_url`` the user entered.
    """
    if not target.auto_append_path:
        url = target.request_url or target.base_url
        return {"base_url": url, "http_client": fixed_http_client(url)}
    return {"base_url": target.sdk_base_url or default_base_url}


class ProtocolAdapter:
    """Turns canonical messages into one provider protocol and streams the answer.

    Adapters are stateless; ``LLMClient`` owns client caching and retries.
    """

    protocol: str = ""
    #: Request fields that are "nice to have": dropped together when the upstream
    #: explicitly rejects one of them with a 400/422.
    optional_params: tuple[str, ...] = ()
    #: Lower-case substrings of the upstream error text that identify such a rejection.
    rejection_markers: tuple[str, ...] = ()

    def create_client(self, target: Target, timeout: float) -> Any:  # pragma: no cover
        raise NotImplementedError

    def build_request(
        self,
        target: Target,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
        *,
        max_tokens: int,
        temperature: float | None,
    ) -> dict[str, Any]:  # pragma: no cover
        raise NotImplementedError

    async def consume(
        self,
        client: Any,
        request: dict[str, Any],
        on_chunk: StreamCallback | None,
        progress: StreamProgress | None,
    ) -> LLMResponse:  # pragma: no cover
        raise NotImplementedError

    def estimate_prompt_tokens(self, request: dict[str, Any]) -> int:
        from app.llm.common import estimate_prompt_tokens

        return estimate_prompt_tokens(
            request.get("messages"), request.get("input"), request.get("system"),
            request.get("instructions"), request.get("tools"),
        )

    def downgrade(self, exc: BaseException, request: dict[str, Any]) -> bool:
        """Strip optional parameters the upstream rejected; True if the request changed."""
        if not is_status_error(exc) or status_code_of(exc) not in (400, 422):
            return False
        if not any(param in request for param in self.optional_params):
            return False
        text = error_text(exc)
        if not any(marker in text for marker in self.rejection_markers):
            return False
        for param in self.optional_params:
            request.pop(param, None)
        return True
