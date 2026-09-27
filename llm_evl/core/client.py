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
from collections.abc import Awaitable, Callable

import httpx

from .models import ErrorType, RequestResult, Target
from .tokenizer import count_tokens

logger = logging.getLogger(__name__)

# A token is roughly 4 chars; we record an ITL sample per content delta chunk.
# Some providers send one token per chunk; others batch. We treat each chunk
# boundary as an ITL observation (the most honest unit the wire gives us).

# Quality component weights. When a prompt defines reference_points, points
# become the primary signal (0.5) and the remaining 0.5 is split between the
# legacy keyword/length checks. With no points, the original 60/40 split is
# preserved exactly so existing scores stay comparable.
W_POINTS = 0.5
W_KEYWORD = 0.6
W_LENGTH = 0.4


def _compute_cost(target: Target, input_tokens: int, output_tokens: int) -> float | None:
    """Cost in CNY. None unless the target has a usable price pair."""
    if target.price_in is None and target.price_out is None:
        return None
    cost = 0.0
    if target.price_in:
        cost += input_tokens / 1_000_000 * target.price_in
    if target.price_out:
        cost += output_tokens / 1_000_000 * target.price_out
    return cost


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
    reference_points: list[str] | None = None,
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
                result.error_type = ErrorType.from_status(resp.status_code).value
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
                    # Previously swallowed silently, so a client-side stream
                    # bug looked identical to a healthy short response.
                    result.malformed_chunks += 1
                    continue

                # Capture usage from the final chunk (OpenAI sends it on the
                # last chunk when include_usage=true).
                u = chunk.get("usage")
                if u:
                    usage = u

                choices = chunk.get("choices") or []
                if not choices:
                    continue
                fr = choices[0].get("finish_reason")
                if fr:
                    result.finish_reason = fr
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
        result.error_type = ErrorType.TIMEOUT.value
        result.ok = False
        result.e2e = time.perf_counter() - t0
        return result
    except httpx.HTTPError as exc:
        result.error = f"{target.name} transport error ({url}): {exc}"
        result.error_type = ErrorType.TRANSPORT.value
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
        if result.finish_reason == "content_filter":
            result.error = result.error or f"{target.name} response blocked by content filter"
            result.error_type = ErrorType.CONTENT_FILTERED.value
            result.ok = False
            return result
        if not text and not usage:
            result.error = result.error or "no content received"
            result.error_type = (
                ErrorType.PARSE.value
                if result.malformed_chunks
                else ErrorType.NO_CONTENT.value
            )
            result.ok = False
            return result

    n_tokens, source = count_tokens(text, usage)
    result.output_tokens = n_tokens
    result.token_source = source

    # Input tokens were previously never captured, so cost was uncomputable.
    if usage:
        in_tok = usage.get("prompt_tokens") or usage.get("input_tokens") or 0
    else:
        in_tok = count_tokens(prompt_text, None)[0]
    result.input_tokens = int(in_tok)
    result.cost = _compute_cost(target, result.input_tokens, n_tokens)

    if result.generation_duration and result.generation_duration > 0 and n_tokens:
        result.tokens_per_second = n_tokens / result.generation_duration
    elif result.generation_duration == 0 and n_tokens:
        # Degenerate: a single chunk carried all content.
        result.tokens_per_second = None

    # Quality scoring (opt-in via reference_points / keywords / min tokens).
    result.output_text = text[:4000]  # cap storage
    _score_quality(result, text, n_tokens, expected_keywords, min_output_tokens,
                   reference_points)

    result.ok = True
    return result


def _score_quality(result: RequestResult, text: str, n_tokens: int,
                   expected_keywords: list[str] | None,
                   min_output_tokens: int | None,
                   reference_points: list[str] | None = None) -> None:
    """Compute quality_score in [0,1]. None if no criteria configured.

    Two modes, so old scores stay comparable:

    - No reference_points: the original behaviour exactly (keywords 60%,
      length 40%).
    - With reference_points: points carry W_POINTS and the remainder is
      split across the legacy checks, then renormalised over the components
      that are actually present.
    """
    has_kw = bool(expected_keywords)
    has_len = min_output_tokens is not None and min_output_tokens > 0
    points = [p for p in (reference_points or []) if p and p.strip()]
    has_pts = bool(points)
    if not has_kw and not has_len and not has_pts:
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

    point_ratio = 1.0
    if has_pts:
        hits = sum(1 for p in points if p in text)
        result.quality_point_hits = hits
        result.quality_point_total = len(points)
        point_ratio = hits / len(points)
        result.quality_point_recall = point_ratio

    if not has_pts:
        # Legacy path — unchanged weights (existing tests depend on this).
        if has_kw and has_len:
            result.quality_score = W_KEYWORD * keyword_ratio + W_LENGTH * length_ratio
        elif has_kw:
            result.quality_score = keyword_ratio
        else:
            result.quality_score = length_ratio
        return

    # Points present: distribute the leftover 0.5 across the legacy checks.
    weights: list[tuple[float, float]] = [(W_POINTS, point_ratio)]
    rest = 1.0 - W_POINTS
    if has_kw and has_len:
        weights.append((rest * W_KEYWORD / (W_KEYWORD + W_LENGTH), keyword_ratio))
        weights.append((rest * W_LENGTH / (W_KEYWORD + W_LENGTH), length_ratio))
    elif has_kw:
        weights.append((rest, keyword_ratio))
    elif has_len:
        weights.append((rest, length_ratio))

    total_w = sum(w for w, _ in weights)
    result.quality_score = sum(w * v for w, v in weights) / total_w


async def stream_chat(
    client: httpx.AsyncClient,
    target: Target,
    messages: list[dict],
    *,
    temperature: float,
    timeout: float,
    on_token: Callable[[str], Awaitable[None]] | None = None,
) -> RequestResult:
    """Send a streaming chat completion with multi-turn messages.

    Calls on_token(text) for each content delta as it arrives.
    Returns a RequestResult with TTFT, tok/s, e2e metrics.
    """
    url = target.base_url.rstrip("/") + "/chat/completions"
    temp = target.temperature if target.temperature is not None else temperature
    payload: dict = {
        "model": target.model,
        "messages": messages,
        "stream": True,
        "temperature": temp,
        "stream_options": {"include_usage": True},
    }
    headers = {
        "Authorization": f"Bearer {target.resolved_api_key()}",
        "Content-Type": "application/json",
        "Accept": "text/event-stream",
    }

    result = RequestResult(target=target.name, prompt_id="chat", concurrency=1)
    full_text_parts: list[str] = []
    usage: dict | None = None
    t0 = result.started_at = time.perf_counter()
    first_token_time: float | None = None
    last_token_time: float | None = None

    try:
        async with client.stream(
            "POST", url, json=payload, headers=headers, timeout=timeout
        ) as resp:
            if resp.status_code >= 400:
                body = await resp.aread()
                body_text = body[:300].decode("utf-8", "replace").strip()
                result.error = f"HTTP {resp.status_code} from {target.name}: {body_text}"
                result.error_type = ErrorType.from_status(resp.status_code).value
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
                    result.malformed_chunks += 1
                    continue

                u = chunk.get("usage")
                if u:
                    usage = u

                choices = chunk.get("choices") or []
                if not choices:
                    continue
                fr = choices[0].get("finish_reason")
                if fr:
                    result.finish_reason = fr
                delta = choices[0].get("delta") or {}
                content = delta.get("content")
                if content:
                    now = time.perf_counter()
                    if first_token_time is None:
                        first_token_time = now
                    else:
                        if last_token_time is not None:
                            result.itl.append(now - last_token_time)
                    last_token_time = now
                    full_text_parts.append(content)
                    if on_token:
                        await on_token(content)

    except httpx.TimeoutException:
        result.timed_out = True
        result.error = f"{target.name} timeout after {timeout}s"
        result.error_type = ErrorType.TIMEOUT.value
        result.ok = False
        result.e2e = time.perf_counter() - t0
        return result
    except httpx.HTTPError as exc:
        result.error = f"{target.name} transport error: {exc}"
        result.error_type = ErrorType.TRANSPORT.value
        result.ok = False
        result.e2e = time.perf_counter() - t0
        return result

    t_end = time.perf_counter()
    text = "".join(full_text_parts)

    if first_token_time is not None:
        result.ttft = first_token_time - t0
        result.e2e = t_end - t0
        result.generation_duration = (t_end - first_token_time) or 0.0
    else:
        result.e2e = t_end - t0
        if result.finish_reason == "content_filter":
            result.error = result.error or f"{target.name} response blocked by content filter"
            result.error_type = ErrorType.CONTENT_FILTERED.value
            result.ok = False
            return result
        if not text and not usage:
            result.error = result.error or "no content received"
            result.error_type = (
                ErrorType.PARSE.value
                if result.malformed_chunks
                else ErrorType.NO_CONTENT.value
            )
            result.ok = False
            return result

    n_tokens, source = count_tokens(text, usage)
    result.output_tokens = n_tokens
    result.token_source = source
    if usage:
        in_tok = usage.get("prompt_tokens") or usage.get("input_tokens") or 0
    else:
        in_tok = 0
    result.input_tokens = int(in_tok)
    result.cost = _compute_cost(target, result.input_tokens, n_tokens)

    if result.generation_duration and result.generation_duration > 0 and n_tokens:
        result.tokens_per_second = n_tokens / result.generation_duration

    result.output_text = text[:4000]
    result.ok = True
    return result
