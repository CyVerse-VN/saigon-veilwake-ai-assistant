"""REST boundary independent of SDK schema versions.

Schema: https://ai.google.dev/static/api/interactions.openapi.json
"""

from __future__ import annotations

from typing import Any

import httpx

from app.integrations.errors import ProviderRequestError

INTERACTIONS_URL = "https://generativelanguage.googleapis.com/v1beta/interactions"


def post_json(
    api_key: str,
    url: str,
    payload: dict[str, Any],
    timeout_seconds: int,
    *,
    transport: httpx.BaseTransport | None = None,
) -> dict[str, Any]:
    """No retries or raw HTTP errors: each call makes at most one provider request."""
    try:
        with httpx.Client(timeout=timeout_seconds, transport=transport) as client:
            response = client.post(url, headers={"x-goog-api-key": api_key}, json=payload)
        if response.is_error:
            hints = {
                400: "Provider rejected the request/model configuration.",
                401: "Check GEMINI_API_KEY.",
                403: "Check API permissions and model access.",
                404: "Model or endpoint is unavailable to this project.",
                429: "Quota/rate limit exceeded; check AI Studio limits.",
            }
            raise ProviderRequestError(
                f"Gemini HTTP {response.status_code}. "
                + hints.get(response.status_code, "Provider request failed; retry later.")
            )
        data = response.json()
        if not isinstance(data, dict):
            raise ValueError("Expected JSON object")
        return data
    except ProviderRequestError:
        raise
    except httpx.TimeoutException:
        raise ProviderRequestError("Gemini request timed out.") from None
    except httpx.RequestError:
        raise ProviderRequestError("Cannot connect to Gemini; check network/proxy.") from None
    except (ValueError, TypeError):
        raise ProviderRequestError("Gemini returned an invalid JSON response.") from None


def output_blocks(response: dict[str, Any]) -> list[dict[str, Any]]:
    """Current REST uses steps/model_output; older Interactions responses use outputs."""
    blocks = []
    steps = response.get("steps")
    for step in steps if isinstance(steps, list) else []:
        if isinstance(step, dict) and step.get("type") == "model_output":
            content = step.get("content")
            if isinstance(content, list):
                blocks.extend(item for item in content if isinstance(item, dict))
    if blocks:
        return blocks
    outputs = response.get("outputs")
    return [item for item in outputs if isinstance(item, dict)] if isinstance(outputs, list) else []


def response_diagnostics(response: dict[str, Any]) -> dict[str, str | int]:
    """Allowlisted status/counts only: no text, URIs, IDs, bodies, or request metadata."""
    states = {"completed", "failed", "cancelled", "incomplete", "in_progress", "requires_action"}
    status = response.get("status")
    usage = response.get("usage") or {}
    if not isinstance(usage, dict):
        usage = {}
    result: dict[str, str | int] = {
        "status": status if isinstance(status, str) and status in states else "unknown",
        "output_blocks": len(output_blocks(response)),
    }
    for field in ("total_input_tokens", "total_output_tokens"):
        value = usage.get(field)
        if type(value) is int and value >= 0:
            result[field] = value
    modalities = usage.get("input_tokens_by_modality") or []
    if isinstance(modalities, list):
        counts = [
            item["tokens"]
            for item in modalities
            if isinstance(item, dict)
            and item.get("modality") in {"audio", "AUDIO"}
            and type(item.get("tokens")) is int
            and item["tokens"] >= 0
        ]
        if counts:
            result["audio_input_tokens"] = sum(counts)
    return result


def require_completed(response: dict[str, Any]) -> None:
    if response.get("status") != "completed":
        status = response_diagnostics(response)["status"]
        raise ProviderRequestError(f"Gemini interaction did not complete (status={status}).")
