"""Token counting: prefer provider usage field, fall back to local tiktoken.

Per Q5: hybrid strategy. The streaming client captures the `usage` object from
the final chunk (when stream_options.include_usage=true). If absent, we count
locally with tiktoken (cl100k_base) as an approximation and tag the source.
"""

from __future__ import annotations

import logging

from .models import TokenSource

logger = logging.getLogger(__name__)

# tiktoken is lazy-loaded so import-time failures don't crash the package.
_tiktoken_enc = None


def _get_encoder():
    global _tiktoken_enc
    if _tiktoken_enc is None:
        import tiktoken

        _tiktoken_enc = tiktoken.get_encoding("cl100k_base")
    return _tiktoken_enc


def count_tokens(text: str, usage: dict | None) -> tuple[int, str]:
    """Return (token_count, source).

    - If usage dict carries completion_tokens, use it (source=usage).
    - Else fall back to tiktoken count (source=tiktoken).
    - Empty text with no usage -> (0, none).
    """
    if usage:
        n = (
            usage.get("completion_tokens")
            or usage.get("output_tokens")        # Anthropic-style alias
            or 0
        )
        if n:
            return int(n), TokenSource.USAGE.value

    if not text:
        return 0, TokenSource.NONE.value

    try:
        enc = _get_encoder()
        return len(enc.encode(text)), TokenSource.TIKTOKEN.value
    except Exception as exc:  # pragma: no cover - tiktoken failure is rare
        logger.warning("tiktoken count failed: %s; estimating by whitespace", exc)
        # Last-resort heuristic: ~1 token per 4 chars.
        return max(1, len(text) // 4), TokenSource.TIKTOKEN.value
