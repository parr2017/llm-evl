"""Mock OpenAI-compatible server for end-to-end testing.

The default ``app`` is the healthy one: it streams a handful of token chunks
with small delays (so TTFT / ITL are non-trivial) and sends usage on the final
chunk. Supports concurrency and non-streaming requests.

``make_app(fault=...)`` builds a server that fails every request a specific
way. The relay tests need *two* servers — one sick, one healthy, both offering
the same model name — because "try the next provider" can only be proven when
the two providers actually behave differently.

Faults: ``"429"``, ``"500"``, ``"400"``, ``"empty"``, ``"junk"``, ``"hang"``.

Model-name faults (``mock-429`` etc.) are also honoured on the healthy server,
for tests that need a failure without standing up a second server.
"""

import asyncio
import json
import time

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse


def make_app(fault: str = "") -> FastAPI:
    application = FastAPI()

    @application.get("/health")
    def health():
        return {"ok": True, "fault": fault, "time": time.time()}

    @application.post("/v1/chat/completions")
    async def chat(req: Request):
        body = await req.json()
        model = body.get("model", "mock")
        prompt = (body.get("messages") or [{}])[0].get("content", "")
        n_tokens = 12 if len(prompt) < 60 else 40 if len(prompt) < 200 else 80
        usage = {"prompt_tokens": 10, "completion_tokens": n_tokens,
                 "total_tokens": 10 + n_tokens}

        # A model name can request a fault on any server, so a single server
        # can also play the "sick provider" role.
        fault_kind = fault
        named_faults = ("mock-429", "mock-500", "mock-400",
                        "mock-empty", "mock-junk")
        if model in named_faults and not fault_kind:
            fault_kind = model[len("mock-"):]

        if fault_kind in ("429", "500", "400"):
            status = int(fault_kind)
            return JSONResponse(
                status_code=status,
                content={"error": {"message": f"injected {status}",
                                   "type": "mock_fault"}},
            )
        if fault_kind == "hang":
            await asyncio.sleep(30)
            return JSONResponse({"error": "too late"})

        if fault_kind == "empty":
            async def empty_gen():
                yield "data: [DONE]\n\n"
            return StreamingResponse(empty_gen(), media_type="text/event-stream")

        if not body.get("stream"):
            return JSONResponse({
                "id": "chatcmpl-mock",
                "object": "chat.completion",
                "model": model,
                "choices": [{
                    "index": 0,
                    "message": {"role": "assistant",
                                "content": " ".join(f"tok{i}" for i in range(n_tokens))},
                    "finish_reason": "stop",
                }],
                "usage": usage,
            })

        async def gen():
            if fault_kind == "junk":
                # A line that is not valid JSON. The relay must forward it
                # untouched and still deliver the real answer behind it.
                yield "data: {not json at all\n\n"
            await asyncio.sleep(0.02)  # simulate TTFT
            for i in range(n_tokens):
                chunk = {
                    "model": model,
                    "choices": [{"index": 0, "delta": {"content": f"tok{i} "},
                                 "finish_reason": None}],
                }
                yield f"data: {json.dumps(chunk)}\n\n"
                await asyncio.sleep(0.005)  # inter-token latency
            done = {
                "model": model,
                "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                "usage": usage,
            }
            yield f"data: {json.dumps(done)}\n\n"
            yield "data: [DONE]\n\n"

        return StreamingResponse(gen(), media_type="text/event-stream")

    @application.get("/v1/models")
    def list_models():
        return {
            "object": "list",
            "data": [
                # vLLM-style: advertises max_model_len
                {"id": "mock-model", "object": "model", "created": 0,
                 "owned_by": "mock", "max_model_len": 32768},
                # alias style: context_length
                {"id": "mock-model-long", "object": "model", "created": 0,
                 "owned_by": "mock", "context_length": 131072},
                # plain: no context advertised
                {"id": "mock-model-plain", "object": "model", "created": 0,
                 "owned_by": "mock"},
            ],
        }

    return application


app = make_app()


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=9999, log_level="warning")
