"""httpx streaming OpenAI-compatible client with TTFT / ITL timing.

Per Q2/Q3/Q5: stream=true is mandatory; one adapter covers OpenAI/DeepSeek/
vLLM/Ollama. TTFT = time from request send to first content chunk. ITL = gap
between consecutive content chunks (one entry per token-bearing delta after the
first). Token count prefers the usage field, falling back to tiktoken.
"""

from __future__ import annotations

import json
import logging
import time

import httpx

from .models import RequestResult, Target
from .tokenizer import count_tokens

logger = logging.getLogger(__name__)

# A token is roughly 4 chars; we record an ITL sample per content delta chunk.
# Some providers send one token per chunk; others batch. We treat each chunk
# boundary as an ITL observation (the most honest unit the wire gives us).


async def stream_request(
    client: httpx.AsyncClient,
    target: Target,
    prompt_text: str,
    *,
    temperature: float,
    timeout: float,
    include_usage: bool,
    prompt_id: str,
    concurrency: int,
    expected_keywords: list[str] | None = None,
    min_output_tokens: int | None = None,
) -> RequestResult:
    """Send one streaming chat completion and return a timed RequestResult.

    Tracks TTFT (first token), TTS (first sentence boundary), ITL, and an
    opt-in quality_score based on expected_keywords hit ratio + length.
    """
    url = target.base_url.rstrip("/") + "/chat/completions"
    payload: dict = {
        "model": target.model,
        "messages": [{"role": "user", "content": prompt_text}],
        "stream": True,
        "temperature": temperature,
    }
    if include_usage:
        payload["stream_options"] = {"include_usage": True}

    headers = {
        "Authorization": f"Bearer {target.resolved_api_key()}",
        "Content-Type": "application/json",
        "Accept": "text/event-stream",
    }

    result = RequestResult(
        target=target.name,
        prompt_id=prompt_id,
        concurrency=concurrency,
    )

    full_text_parts: list[str] = []
    usage: dict | None = None
    t0 = result.started_at = time.perf_counter()
    first_token_time: float | None = None
    first_sentence_time: float | None = None
    last_token_time: float | None = None
    # Sentence boundary chars: CJK + ASCII punctuation, plus newline.
    sentence_endings = set("。.!?!?\n")

    try:
        async with client.stream(
            "POST", url, json=payload, headers=headers, timeout=timeout
        ) as resp:
            if resp.status_code >= 400:
                body = await resp.aread()
                body_text = body[:300].decode("utf-8", "replace").strip()
                result.error = f"HTTP {resp.status_code} from {target.name} ({url}): {body_text}"
                result.ok = False
                result.e2e = time.perf_counter() - t0
                return result

            async for line in resp.aiter_lines():
                if not line:
                    continue
                if line.startswith("data:"):
                    line = line[len("data:"):].lstrip()
                if line.strip() == "[DONE]":
                    break
                try:
                    chunk = json.loads(line)
                except json.JSONDecodeError:
                    continue

                # Capture usage from the final chunk (OpenAI sends it on the
                # last chunk when include_usage=true).
                u = chunk.get("usage")
                if u:
                    usage = u

                choices = chunk.get("choices") or []
                if not choices:
                    continue
                delta = choices[0].get("delta") or {}
                content = delta.get("content")
                if content:
                    now = time.perf_counter()
                    if first_token_time is None:
                        first_token_time = now
                    else:
                        # ITL between consecutive content chunks.
                        if last_token_time is not None:
                            result.itl.append(now - last_token_time)
                    last_token_time = now
                    full_text_parts.append(content)
                    # First-sentence boundary: only meaningful after the first
                    # token, so we don't conflate TTS with TTFT.
                    if first_sentence_time is None and first_token_time is not None:
                        if any(ch in sentence_endings for ch in content):
                            first_sentence_time = now

    except httpx.TimeoutException:
        result.timed_out = True
        result.error = f"{target.name} timeout after {timeout}s ({url})"
        result.ok = False
        result.e2e = time.perf_counter() - t0
        return result
    except httpx.HTTPError as exc:
        result.error = f"{target.name} transport error ({url}): {exc}"
        result.ok = False
        result.e2e = time.perf_counter() - t0
        return result

    t_end = time.perf_counter()
    text = "".join(full_text_parts)

    if first_token_time is not None:
        result.ttft = first_token_time - t0
        result.e2e = t_end - t0
        result.generation_duration = (t_end - first_token_time) or 0.0
        if first_sentence_time is not None:
            result.tts = first_sentence_time - t0
    else:
        # No content token received: treat as error unless usage says otherwise.
        result.e2e = t_end - t0
        if not text and not usage:
            result.error = result.error or "no content received"
            result.ok = False
            return result

    n_tokens, source = count_tokens(text, usage)
    result.output_tokens = n_tokens
    result.token_source = source

    if result.generation_duration and result.generation_duration > 0 and n_tokens:
        result.tokens_per_second = n_tokens / result.generation_duration
    elif result.generation_duration == 0 and n_tokens:
        # Degenerate: a single chunk carried all content.
        result.tokens_per_second = None

    # Quality scoring (opt-in via expected_keywords / min_output_tokens).
    result.output_text = text[:4000]  # cap storage
    _score_quality(result, text, n_tokens, expected_keywords, min_output_tokens)

    result.ok = True
    return result


def _score_quality(result: RequestResult, text: str, n_tokens: int,
                   expected_keywords: list[str] | None,
                   min_output_tokens: int | None) -> None:
    """Compute quality_score in [0,1]. None if no criteria configured."""
    has_kw = bool(expected_keywords)
    has_len = min_output_tokens is not None and min_output_tokens > 0
    if not has_kw and not has_len:
        return  # quality not measured for this prompt

    keyword_ratio = 1.0
    if has_kw:
        hits = sum(1 for kw in expected_keywords if kw and kw in text)
        result.quality_keyword_hits = hits
        keyword_ratio = hits / len(expected_keywords) if expected_keywords else 1.0

    length_ratio = 1.0
    if has_len and min_output_tokens:
        length_ratio = min(1.0, n_tokens / min_output_tokens)
        result.quality_length_ok = n_tokens >= min_output_tokens

    # Weight: keywords 60%, length 40% (if both); else the single available one.
    if has_kw and has_len:
        result.quality_score = 0.6 * keyword_ratio + 0.4 * length_ratio
    elif has_kw:
        result.quality_score = keyword_ratio
    else:
        result.quality_score = length_ratio
