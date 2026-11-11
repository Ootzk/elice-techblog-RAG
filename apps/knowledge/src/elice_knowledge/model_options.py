"""Validate per-execution model options without accepting credentials or transport overrides."""

import json
import re
from typing import Any

RESERVED = {
    "model",
    "messages",
    "input",
    "encoding_format",
    "api_key",
    "base_url",
    "headers",
    "authorization",
    "response_format",
    "stream_options",
}


def validate_options(value: dict[str, Any] | None, *, reserved=RESERVED) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict) or len(value) > 32:
        raise ValueError("model_options_invalid")

    def check(item):
        if isinstance(item, dict):
            for key, child in item.items():
                if (
                    not isinstance(key, str)
                    or not re.fullmatch(r"[a-zA-Z][a-zA-Z0-9_]{0,63}", key)
                    or key.lower() in reserved
                    or any(x in key.lower() for x in ("password", "secret", "token_key"))
                ):
                    raise ValueError("model_option_forbidden")
                check(child)
        elif isinstance(item, list):
            for child in item:
                check(child)

    check(value)
    try:
        encoded = json.dumps(value, allow_nan=False, ensure_ascii=False)
    except (TypeError, ValueError):
        raise ValueError("model_options_invalid") from None
    if len(encoded) > 10000:
        raise ValueError("model_options_too_large")
    return json.loads(encoded)


def validate_generation_options(value):
    return validate_options(value, reserved=RESERVED | {"stream"})


def validate_embedding_options(value):
    return validate_options(value, reserved=RESERVED | {"stream", "truncate"})


def validate_judge_options(value):
    result = validate_options(value)
    if "stream" in result and type(result["stream"]) is not bool:
        raise ValueError("judge_stream_must_be_boolean")
    if "max_tokens" in result and "max_completion_tokens" in result:
        raise ValueError("judge_output_limits_conflict")
    return result
