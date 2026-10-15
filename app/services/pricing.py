import json
import math
from collections.abc import Sequence
from typing import Any

PER_MESSAGE_PROTOCOL_BYTES = 8
FIXED_PROTOCOL_BYTES = 16


def estimate_input_tokens(messages: Sequence[Any], safety_factor: float = 1.25) -> int:
    """Return a conservative byte-level upper bound for serialized chat messages."""
    serializable_messages = [
        message.model_dump() if hasattr(message, "model_dump") else message
        for message in messages
    ]
    serialized = json.dumps(
        serializable_messages,
        ensure_ascii=False,
        separators=(",", ":"),
    )
    estimated_bytes = (
        len(serialized.encode("utf-8"))
        + len(messages) * PER_MESSAGE_PROTOCOL_BYTES
        + FIXED_PROTOCOL_BYTES
    )
    return max(1, math.ceil(estimated_bytes * safety_factor))


def calculate_cost_micro_cny(
    input_tokens: int,
    output_tokens: int,
    input_rate_micro_cny_per_million: int,
    output_rate_micro_cny_per_million: int,
) -> int:
    input_cost = math.ceil(input_tokens * input_rate_micro_cny_per_million / 1_000_000)
    output_cost = math.ceil(output_tokens * output_rate_micro_cny_per_million / 1_000_000)
    return input_cost + output_cost
