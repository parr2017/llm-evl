"""End-to-end check: a real server, a real client, two upstreams, two ports.

Why this exists separately from pytest: the point of a relay is that a client
that knows nothing about it can talk to it. So this script starts the actual
FastAPI apps with uvicorn, points a plain ``httpx`` client at ``/v1`` — exactly
what an OpenAI SDK does — and checks the login gate, the client keys, the
groups, the retry, the circuit breaker and the call log.

It also checks the *separation*: the LAN-facing port must serve relay calls
and nothing else (no management endpoints, no UI, no way to mint a key), while
the management port stays on loopback.

It writes a temporary relay.yaml / targets.yaml, so nothing in the repo is
touched. Run directly:
    .venv/Scripts/python.exe scripts/e2e_relay.py
"""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
import threading
import time
from pathlib import Path

import httpx
import uvicorn
import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from llm_evl.api.server import create_app, create_relay_app   # noqa: E402
from llm_evl.core.auth import hash_password                  # noqa: E402
from mock_llm_server import make_app                         # noqa: E402

GOOD_PORT = 9985
SICK_PORT = 9984
MGMT_PORT = 9983
RELAY_PORT = 9982

ADMIN_USER = "admin"
ADMIN_PASSWORD = "e2e-admin-password"

results: list[tuple[bool, str, str]] = []


def check(ok: bool, name: str, detail: str = "") -> None:
    results.append((ok, name, detail))
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"  — {detail}" if detail else ""))


def _serve(app, port) -> uvicorn.Server:
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port,
                                           log_level="error"))
    threading.Thread(target=server.run, daemon=True).start()
    deadline = time.time() + 10
    while time.time() < deadline:
        try:
            if httpx.get(f"http://127.0.0.1:{port}/health", timeout=1).is_success:
                break
        except Exception:
            time.sleep(0.1)
    return server


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="llm-evl-relay-"))
    good = _serve(make_app(), GOOD_PORT)
    sick = _serve(make_app(fault="429"), SICK_PORT)

    targets = tmp / "targets.yaml"
    targets.write_text(yaml.safe_dump({"providers": [
        {"name": "a-sick", "base_url": f"http://127.0.0.1:{SICK_PORT}/v1",
         "api_key": "k", "models": [{"name": "shared-model"}]},
        {"name": "b-healthy", "base_url": f"http://127.0.0.1:{GOOD_PORT}/v1",
         "api_key": "k", "models": [{"name": "shared-model"},
                                    {"name": "only-here"}]},
    ]}), encoding="utf-8")
    relay = tmp / "relay.yaml"
    relay.write_text(yaml.safe_dump({
        "relay": {
            "enabled": False, "strategy": "round_robin", "failure_threshold": 1,
            "cooldown_seconds": 60, "max_retries": 2,
        },
        "auth": {
            "admin": {"username": ADMIN_USER,
                      "password_hash": hash_password(ADMIN_PASSWORD)},
            "session_secret": "e2e-session-secret",
            "session_hours": 12,
        },
    }), encoding="utf-8")

    # Two servers, two ports — exactly what `llm-evl serve` does.
    mgmt = _serve(create_app(str(targets), str(relay)), MGMT_PORT)
    relay_server = _serve(create_relay_app(str(targets), str(relay)), RELAY_PORT)
    base = f"http://127.0.0.1:{RELAY_PORT}"     # LAN-facing relay port
    admin = f"http://127.0.0.1:{MGMT_PORT}"    # loopback-only management port
    client = httpx.Client(timeout=30)
    key_hdr: dict[str, str] = {}
    body = {"model": "shared-model",
            "messages": [{"role": "user", "content": "hello"}]}

    try:
        print("\n[0] 管理端登录门禁")
        r = client.get(f"{admin}/api/providers")
        check(r.status_code == 401, "未登录访问 /api/providers 被拒", f"got {r.status_code}")
        r = client.get(f"{admin}/api/providers", headers={"Accept": "text/html"})
        check(r.status_code == 302, "浏览器导航被重定向到登录页", f"got {r.status_code}")
        r = client.get(f"{admin}/login")
        check(r.status_code == 200 and "登录" in r.text, "登录页可公开访问")
        r = client.post(f"{admin}/api/auth/login",
                        json={"username": ADMIN_USER, "password": "wrong"})
        check(r.status_code == 401, "错误密码被拒", f"got {r.status_code}")
        r = client.post(f"{admin}/api/auth/login",
                        json={"username": ADMIN_USER, "password": ADMIN_PASSWORD})
        check(r.status_code == 200, "正确密码登录成功", f"got {r.status_code}")
        r = client.get(f"{admin}/api/providers")
        check(r.status_code == 200, "登录后可访问 /api/providers")

        print("\n[0b] 两个端口的隔离（安全边界）")
        r = client.get(f"{base}/api/relay/config")
        check(r.status_code == 404, "中转端口没有管理接口", f"got {r.status_code}")
        r = client.post(f"{base}/api/relay/clients", json={"name": "intruder"})
        check(r.status_code == 404, "中转端口无法自行创建 key（关键）", f"got {r.status_code}")
        r = client.get(f"{base}/")
        check(r.status_code == 404, "中转端口没有 Web UI", f"got {r.status_code}")

        print("\n[1] 未开启中转 / 还没有 key 时拒绝转发")
        r = client.get(f"{base}/v1/models")
        check(r.status_code == 403, "未开启时 /v1/models 返回 403", f"got {r.status_code}")
        r = client.post(f"{base}/v1/chat/completions", json=body)
        check(r.status_code == 403, "未开启时 /v1/chat/completions 返回 403",
              f"got {r.status_code}")

        print("\n[2] 开启中转 + 创建客户端 key（模拟 UI 操作）")
        r = client.post(f"{admin}/api/relay/config",
                        json={"enabled": True, "failure_threshold": 1})
        check(r.status_code == 200 and r.json()["config"]["enabled"] is True,
              "开关已保存", f"HTTP {r.status_code}")
        check(yaml.safe_load(relay.read_text(encoding="utf-8"))["relay"]["enabled"] is True,
              "配置落盘 (relay.yaml)")
        r = client.get(f"{base}/v1/models")
        check(r.status_code == 401 and r.json()["error"]["code"] == "no_client_keys",
              "还没有 key 时返回 401 no_client_keys", f"got {r.status_code}")
        r = client.post(f"{admin}/api/relay/clients", json={"name": "e2e-user"})
        check(r.status_code == 200, "创建客户端 key", f"HTTP {r.status_code}")
        relay_key = r.json().get("key", "")
        check(relay_key.startswith("sk-r_"), "key 带 sk-r_ 前缀", relay_key[:8])
        check(relay_key not in relay.read_text(encoding="utf-8"),
              "relay.yaml 里没有明文 key（只存哈希）")
        key_hdr = {"Authorization": f"Bearer {relay_key}"}
        r = client.get(f"{base}/v1/models", headers=key_hdr)
        check(r.status_code == 200, "带 key 后 /v1/models 可用", f"got {r.status_code}")
        r = client.get(f"{base}/v1/models")
        check(r.status_code == 401, "不带 key 仍然被拒", f"got {r.status_code}")

        print("\n[3] /v1/models 合并模型池")
        data = client.get(f"{base}/v1/models", headers=key_hdr).json()
        ids = {m["id"]: m for m in data["data"]}
        check(set(ids) == {"shared-model", "only-here"}, "返回两个模型名",
              str(sorted(ids)))
        check(ids["shared-model"]["n_providers"] == 2, "shared-model 聚合了 2 个供应商",
              str(ids["shared-model"]["providers"]))
        check(ids["only-here"]["n_providers"] == 1, "单供应商模型也在池中")

        print("\n[4] 自建聚合组：外部只写组名 A")
        r = client.post(f"{admin}/api/relay/config", json={"groups": {
            "A": {"description": "日常对话", "strategy": "weighted", "members": [
                {"provider": "a-sick", "model": "shared-model", "weight": 1},
                {"provider": "b-healthy", "model": "shared-model", "weight": 3},
            ]},
        }})
        check(r.status_code == 200, "保存聚合组 A", f"HTTP {r.status_code}")
        ids = [m["id"] for m in client.get(f"{base}/v1/models", headers=key_hdr).json()["data"]]
        check("A" in ids, "A 出现在 /v1/models 里", str(ids))
        r = client.post(f"{base}/v1/chat/completions", json={**body, "model": "A"},
                        headers=key_hdr)
        check(r.status_code == 200, "用组名 A 调用成功", f"HTTP {r.status_code}")
        check(r.json().get("object") == "chat.completion", "上游响应原样透传")
        log = client.get(f"{admin}/api/relay/logs?limit=5").json()["logs"][0]
        check(log["model"] == "A", "日志记录的请求模型是组名 A", log["model"])
        check(log["client"] == "e2e-user", "日志记录了是哪个 key 调的", log["client"])
        check(relay_key not in json.dumps(log), "日志里没有 key 本身")

        print("\n[5] 非流式转发 + 429 自动换供应商")
        r = client.post(f"{base}/v1/chat/completions", json=body, headers=key_hdr)
        check(r.status_code == 200, "最终成功", f"HTTP {r.status_code}")
        check(r.json().get("object") == "chat.completion", "上游响应原样透传",
              str(r.json().get("object")))
        check(r.headers.get("x-relay-provider") == "b-healthy",
              "响应头标出命中供应商", str(r.headers.get("x-relay-provider")))

        print("\n[6] 流式转发（逐块透传）")
        chunks, first = [], None
        t0 = time.perf_counter()
        with client.stream("POST", f"{base}/v1/chat/completions",
                           json={**body, "stream": True}, headers=key_hdr,
                           timeout=30) as resp:
            check(resp.status_code == 200, "流式 200", f"HTTP {resp.status_code}")
            check(resp.headers["content-type"].startswith("text/event-stream"),
                  "Content-Type 是 text/event-stream",
                  resp.headers.get("content-type", ""))
            for line in resp.iter_lines():
                if line.strip() and not chunks:
                    first = time.perf_counter() - t0
                chunks.append(line)
        text = "\n".join(chunks)
        check("[DONE]" in text, "收到上游的 [DONE]")
        check("tok0" in text, "内容块逐块透传")
        check(first is not None and first > 0, "首块延迟可测",
              f"{first:.3f}s" if first else "-")

        print("\n[7] 熔断：坏供应商连续失败后被跳过")
        for _ in range(3):
            client.post(f"{base}/v1/chat/completions", json=body, headers=key_hdr)
        cfg = client.get(f"{admin}/api/relay/config").json()
        states = {m["provider"]: m["breaker"]["state"]
                  for m in cfg["pools"][0]["members"]}
        check(states.get("a-sick") == "open", "a-sick 处于冷却中", str(states))
        check(states.get("b-healthy") == "closed", "b-healthy 仍正常", str(states))
        r = client.post(f"{admin}/api/relay/breakers/reset?provider=a-sick")
        check(r.status_code == 200, "手动复位熔断", f"HTTP {r.status_code}")
        states = {m["provider"]: m["breaker"]["state"]
                  for m in client.get(f"{admin}/api/relay/config").json()["pools"][0]["members"]}
        check(states.get("a-sick") == "closed", "复位后重新参与选路", str(states))

        print("\n[8] key 停用 / 删除 / 组授权")
        r = client.post(f"{admin}/api/relay/clients/e2e-user/enabled?enabled=false")
        check(r.status_code == 200, "停用 key", f"HTTP {r.status_code}")
        r = client.get(f"{base}/v1/models", headers=key_hdr)
        check(r.status_code == 401, "停用后 key 立即失效", f"got {r.status_code}")
        client.post(f"{admin}/api/relay/clients/e2e-user/enabled?enabled=true")
        check(client.get(f"{base}/v1/models", headers=key_hdr).status_code == 200,
              "重新启用后可用")

        r = client.post(f"{admin}/api/relay/clients",
                        json={"name": "scoped", "groups": ["A"]})
        scoped = r.json().get("key", "")
        scoped_hdr = {"Authorization": f"Bearer {scoped}"}
        ids = [m["id"] for m in client.get(f"{base}/v1/models", headers=scoped_hdr).json()["data"]]
        check(ids == ["A"], "限定组的 key 只能看到组 A", str(ids))
        r = client.post(f"{base}/v1/chat/completions", json=body, headers=scoped_hdr)
        check(r.status_code == 403, "越权调用被拒 403", f"got {r.status_code}")

        r = client.delete(f"{admin}/api/relay/clients/scoped")
        check(r.status_code == 200, "删除 key", f"HTTP {r.status_code}")
        check(client.get(f"{base}/v1/models", headers=scoped_hdr).status_code == 401,
              "删除后 key 失效")

        print("\n[9] 调用日志")
        logs = client.get(f"{admin}/api/relay/logs?limit=50").json()
        rows = logs["logs"]
        check(len(rows) >= 4, "记录了每次调用", f"{len(rows)} 条")
        retried = [r_ for r_ in rows if r_.get("retry", 0) > 0]
        check(len(retried) >= 1, "至少一条记录标注了重试", f"{len(retried)} 条")
        streamed = [r_ for r_ in rows if r_.get("stream")]
        check(bool(streamed), "流式调用被标记")
        check(any(r_["ttft"] and r_["ttft"] > 0 for r_ in streamed),
              "流式调用记录了 TTFT")
        check(all("api_key" not in json.dumps(r_) for r_ in rows),
              "日志里没有任何密钥字段")
        filtered = client.get(f"{admin}/api/relay/logs?model=only-here").json()
        check(filtered["logs"] == [], "按模型过滤生效（无该模型调用则为空）")

        print("\n[10] 未知模型 / 旧路由不回归")
        r = client.post(f"{base}/v1/chat/completions", json={**body, "model": "ghost"},
                        headers=key_hdr)
        check(r.status_code == 404 and r.json()["error"]["code"] == "model_not_found",
              "未知模型返回 OpenAI 风格 404", f"HTTP {r.status_code}")
        r = client.get(f"{base}/v1/not-a-route")
        check(r.status_code == 404 and "text/html" not in r.headers.get("content-type", ""),
              "/v1 未知路径返回 JSON 404，不是 SPA 的 index.html",
              r.headers.get("content-type", ""))
        r = client.get(f"{admin}/")
        check(r.status_code == 200 and "llm-evl" in r.text, "SPA 首页仍可访问")
        r = client.get(f"{admin}/api/targets")
        check(r.status_code == 200, "既有 /api/targets 未受影响")
        r = client.post(f"{admin}/api/auth/logout")
        check(r.status_code == 200, "登出成功", f"HTTP {r.status_code}")
        r = client.get(f"{admin}/api/targets")
        check(r.status_code == 401, "登出后管理接口重新锁上", f"got {r.status_code}")
    finally:
        client.close()
        for s in (relay_server, mgmt, good, sick):
            s.should_exit = True
        shutil.rmtree(tmp, ignore_errors=True)

    passed = sum(1 for ok, _, _ in results if ok)
    print(f"\n{'=' * 60}\n  {passed}/{len(results)} passed")
    if passed != len(results):
        print("\n失败项：")
        for ok, name, detail in results:
            if not ok:
                print(f"  - {name}  ({detail})")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
