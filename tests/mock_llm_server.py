"""Mock OpenAI-compatible streaming server for end-to-end testing.

Streams a handful of token chunks with small delays so TTFT / ITL are
non-trivial, then sends usage on the final chunk. Supports concurrency.
"""

import asyncio
import json
import time

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse

app = FastAPI()


@app.post("/v1/chat/completions")
async def chat(req: Request):
    body = await req.json()
    model = body.get("model", "mock")
    # deterministic-ish content, length scaled by prompt
    prompt = (body.get("messages") or [{}])[0].get("content", "")
    n_tokens = 12 if len(prompt) < 60 else 40 if len(prompt) < 200 else 80

    async def gen():
        await asyncio.sleep(0.05)  # simulate TTFT
        for i in range(n_tokens):
            chunk = {
                "model": model,
                "choices": [{"index": 0, "delta": {"content": f"tok{i} "}, "finish_reason": None}],
            }
            yield f"data: {json.dumps(chunk)}\n\n"
            await asyncio.sleep(0.01)  # inter-token latency
        # final chunk with usage
        done = {
            "model": model,
            "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 10, "completion_tokens": n_tokens, "total_tokens": 10 + n_tokens},
        }
        yield f"data: {json.dumps(done)}\n\n"
        yield "data: [DONE]\n\n"

    return StreamingResponse(gen(), media_type="text/event-stream")


@app.get("/health")
def health():
    return {"ok": True, "time": time.time()}


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=9999, log_level="warning")
