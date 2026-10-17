"""The only place that talks to OpenAI.

* Official ``openai`` Python SDK, Responses API, one normal (not Batch) request
  per image, Structured Outputs (``text.format`` json_schema, strict), and
  ``store=False`` so no response object is kept for later retrieval.
* The key is read from settings (environment) here and nowhere else; it is
  never logged, put in an exception message or returned by an endpoint.
* SDK retries are off (``max_retries=0``): retrying is the caller's decision,
  because every attempt is paid for and counted against a budget.
* Every failure is mapped to a small set of exceptions with a short reason code,
  so callers can decide to retry, fall back, or stop without reading provider text.
"""
import json
import logging
from dataclasses import dataclass

from django.conf import settings

from .advisor_schema import SCHEMA_NAME, build_instructions, response_schema

logger = logging.getLogger(__name__)


class AdvisorUnavailable(Exception):
    """Not configured, disabled, or the SDK is missing."""

    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


class TransientProviderError(Exception):
    """Worth another attempt: timeout, connection failure, rate limit, provider 5xx."""

    def __init__(self, reason, retry_after=None):
        super().__init__(reason)
        self.reason = reason
        self.retry_after = retry_after


class ProviderError(Exception):
    """Not worth retrying: rejected key or request, unknown model, exhausted account quota."""

    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


class ReplyRefused(Exception):
    """The model declined to answer (a ``refusal`` content part)."""


class ReplyUnusable(Exception):
    """Incomplete or not valid JSON; ``reason`` says which."""

    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


@dataclass
class ProviderReply:
    payload: dict
    input_tokens: int
    output_tokens: int


def availability():
    """Why the advisor can or cannot be used right now; contains no secret."""
    if not settings.OPENAI_ADVISOR_ENABLED:
        return {"available": False, "reason": "disabled"}
    if not settings.OPENAI_API_KEY:
        return {"available": False, "reason": "not_configured"}
    if not settings.OPENAI_ADVISOR_MODEL:
        return {"available": False, "reason": "model_not_configured"}
    return {"available": True, "reason": ""}


def build_client():
    """A fresh SDK client for this process; raises AdvisorUnavailable when it cannot be used."""
    state = availability()
    if not state["available"]:
        raise AdvisorUnavailable(state["reason"])
    try:
        from openai import OpenAI
    except ImportError as error:
        raise AdvisorUnavailable("sdk_missing") from error
    options = {"api_key": settings.OPENAI_API_KEY, "timeout": settings.OPENAI_ADVISOR_TIMEOUT_SECONDS, "max_retries": 0}
    if settings.OPENAI_BASE_URL:
        options["base_url"] = settings.OPENAI_BASE_URL
    return OpenAI(**options)


def _classify(error):
    """Map an SDK exception to ours without keeping the provider's message."""
    import openai

    if isinstance(error, openai.APITimeoutError):
        return TransientProviderError("timeout")
    if isinstance(error, openai.APIConnectionError):
        return TransientProviderError("connection_error")
    if isinstance(error, openai.RateLimitError):
        body = getattr(error, "body", None)
        code = getattr(error, "code", None) or (body.get("code") if isinstance(body, dict) else None)
        if code == "insufficient_quota":
            return ProviderError("quota_exhausted")
        retry_after = None
        try:
            retry_after = float(error.response.headers.get("retry-after"))
        except (AttributeError, TypeError, ValueError):
            pass
        return TransientProviderError("rate_limited", retry_after=retry_after)
    if isinstance(error, openai.APIStatusError):
        status = getattr(error, "status_code", 0)
        if status >= 500 or status in (408, 409):
            return TransientProviderError("provider_unavailable")
        if status in (401, 403):
            return ProviderError("not_authorized")
        if status == 404:
            return ProviderError("model_unavailable")
        return ProviderError("request_rejected")
    return ProviderError("provider_error")


def _text_of(response):
    """``output_text`` of a Responses reply, or raise ReplyRefused / ReplyUnusable."""
    status = getattr(response, "status", "completed")
    if status == "incomplete":
        raise ReplyUnusable("incomplete")
    if status not in (None, "completed"):
        raise ReplyUnusable(f"status_{status}")
    texts = []
    for item in getattr(response, "output", None) or []:
        if getattr(item, "type", "") != "message":
            continue
        for part in getattr(item, "content", None) or []:
            kind = getattr(part, "type", "")
            if kind == "refusal":
                raise ReplyRefused()
            if kind == "output_text":
                texts.append(getattr(part, "text", "") or "")
    if not texts:
        raise ReplyUnusable("empty")
    return "".join(texts)


def request_advice(client, *, image_data_url, user_text):
    """Send one bounded preview with its measurements; return the parsed reply and its token use."""
    try:
        response = client.responses.create(
            model=settings.OPENAI_ADVISOR_MODEL,
            instructions=build_instructions(),
            input=[{
                "role": "user",
                "content": [
                    {"type": "input_text", "text": user_text},
                    {"type": "input_image", "image_url": image_data_url, "detail": settings.OPENAI_ADVISOR_IMAGE_DETAIL},
                ],
            }],
            text={"format": {"type": "json_schema", "name": SCHEMA_NAME, "strict": True, "schema": response_schema()}},
            max_output_tokens=settings.OPENAI_ADVISOR_MAX_OUTPUT_TOKENS,
            store=False,
        )
    except Exception as error:  # classified; the provider's text is not kept
        classified = _classify(error)
        logger.warning("Advisor request failed: %s (%s)", classified.reason, type(error).__name__)
        raise classified from None
    usage = getattr(response, "usage", None)
    tokens = (int(getattr(usage, "input_tokens", 0) or 0), int(getattr(usage, "output_tokens", 0) or 0))
    try:
        text = _text_of(response)
        payload = json.loads(text)
    except (ReplyRefused, ReplyUnusable) as error:
        error.tokens = tokens
        raise
    except ValueError as error:
        unusable = ReplyUnusable("not_json")
        unusable.tokens = tokens
        raise unusable from error
    return ProviderReply(payload=payload, input_tokens=tokens[0], output_tokens=tokens[1])
