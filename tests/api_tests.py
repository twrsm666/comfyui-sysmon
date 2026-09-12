"""HTTP API tests for the System Monitor routes.

Runs the real :class:`MonitorAPI` handlers against a real aiohttp test server, so
argument parsing, status codes, JSON bodies, masking and error paths are all
exercised the same way ComfyUI would exercise them.

ComfyUI's ``server`` module is stubbed because importing the real one outside a
running ComfyUI builds a second, detached ``PromptServer``.

    python tests/api_tests.py
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import types
from http.server import BaseHTTPRequestHandler, HTTPServer

import aiohttp
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

PASSED = 0
FAILED = 0


def check(label: str, condition: bool, detail: str = "") -> None:
    global PASSED, FAILED
    if condition:
        PASSED += 1
        print(f"  PASS  {label}")
    else:
        FAILED += 1
        print(f"  FAIL  {label}  {detail}")


def section(title: str) -> None:
    print(f"\n=== {title} ===")


# --------------------------------------------------------------------------
# ComfyUI stand-ins
# --------------------------------------------------------------------------
def install_fake_server() -> web.RouteTableDef:
    """Register a fake ``server`` module exposing a PromptServer with routes."""
    routes = web.RouteTableDef()

    class FakePromptServer:
        instance = None

        def __init__(self):
            FakePromptServer.instance = self
            self.routes = routes

        def send_sync(self, event, data, sid=None):
            self.sent = getattr(self, "sent", [])
            self.sent.append((event, data))

    module = types.ModuleType("server")
    module.PromptServer = FakePromptServer  # type: ignore[attr-defined]
    sys.modules["server"] = module
    FakePromptServer()
    return routes


# --------------------------------------------------------------------------
# A tiny OpenAI-compatible endpoint so /analyze and /test_llm can succeed
# --------------------------------------------------------------------------
class _MockLLM(BaseHTTPRequestHandler):
    reply = "## 结论\n运行正常，显存充足。"
    received: list = []

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length).decode("utf-8")
        try:
            _MockLLM.received.append(json.loads(raw))
        except Exception:
            _MockLLM.received.append({})
        payload = {
            "model": "mock-model",
            "choices": [{"message": {"role": "assistant", "content": _MockLLM.reply}}],
            "usage": {"total_tokens": 42},
        }
        body = json.dumps(payload).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


def start_mock_llm() -> tuple[HTTPServer, int]:
    server = HTTPServer(("127.0.0.1", 0), _MockLLM)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, server.server_address[1]


# --------------------------------------------------------------------------
async def run_api_tests() -> None:
    from sysmon.api import MonitorAPI
    from sysmon.config import Config
    from sysmon.metrics import MetricsSampler
    from sysmon.store import RunStore

    workdir = tempfile.mkdtemp(prefix="sysmon_api_")
    mock_llm, llm_port = start_mock_llm()

    try:
        routes = install_fake_server()

        config = Config(os.path.join(workdir, "cfg.json"))
        config.update({
            "sample_interval_ms": 200,
            "sample_interval_active_ms": 200,
            "history_size": 60,
            "llm_provider": "openai_compatible",
            "llm_base_url": f"http://127.0.0.1:{llm_port}",
            "llm_model": "mock-model",
            "llm_api_key": "sk-mock-abcdefghijklmnopqrstuvwxyz",
            "llm_enabled": True,
        })
        sampler = MetricsSampler(config)
        sampler.start()
        store = RunStore(config, sampler, os.path.join(workdir, "logs"))
        api = MonitorAPI(config, sampler, store)
        check("routes register successfully", api.register() is True)

        app = web.Application()
        app.add_routes(routes)
        client = TestClient(TestServer(app))
        await client.start_server()

        # Seed one finished run with a node and an error so /runs, /errors,
        # /context and /analyze all have something to work with.
        store.begin_run("pid-alpha", {"1": {"class_type": "KSampler"},
                                      "2": {"class_type": "VAEDecode"}},
                        {"client_id": "t", "extra_pnginfo": {"workflow": {
                            "name": "api-test-flow",
                            "nodes": [{"id": 1, "type": "KSampler", "title": "采样器"},
                                      {"id": 2, "type": "VAEDecode", "title": "解码"}]}}})
        store.note_node_start("1")
        time.sleep(0.25)
        store.note_node_end("1")
        store.note_node_start("2")
        time.sleep(0.25)
        store.note_node_end("2")
        store.end_run("success", "pid-alpha")

        store.begin_run("pid-beta", {"3": {"class_type": "KSampler"}}, {"client_id": "t"})
        store.note_node_start("3")
        store.note_error({"prompt_id": "pid-beta", "node_id": "3", "node_type": "KSampler",
                          "exception_type": "RuntimeError",
                          "exception_message": "CUDA out of memory",
                          "traceback": ["line1", "line2"], "current_inputs": {"seed": 1}})
        store.end_run("error", "pid-beta")

        # ------------------------------------------------------------------
        section("Read endpoints")
        r = await client.get("/sysmon/state")
        data = await r.json()
        check("GET /state 200", r.status == 200, str(r.status))
        check("state reports ok", data.get("ok") is True)
        check("state carries a live sample", isinstance(data.get("sample"), dict))
        check("state carries sampler stats", isinstance(data.get("sampler"), dict))
        check("state includes version", bool(data.get("version")), str(data.get("version")))
        check("state config is masked, no raw key",
              "sk-mock-abcdefghijklmnopqrstuvwxyz" not in json.dumps(data))

        r = await client.get("/sysmon/series?limit=5")
        data = await r.json()
        check("GET /series 200", r.status == 200)
        check("series honours limit", len(data.get("samples", [])) <= 5,
              str(len(data.get("samples", []))))

        r = await client.get("/sysmon/series?limit=notanumber")
        check("bad limit does not 500", r.status == 200, str(r.status))

        r = await client.get("/sysmon/runs?limit=10")
        data = await r.json()
        check("GET /runs 200", r.status == 200)
        runs = data.get("runs", [])
        check("runs returned newest first",
              len(runs) == 2 and runs[0]["prompt_id"] == "pid-beta",
              str([x["prompt_id"] for x in runs]))

        r = await client.get("/sysmon/run/pid-alpha")
        data = await r.json()
        check("GET /run/{id} 200", r.status == 200)
        check("run detail has nodes", len(data["run"]["nodes"]) == 2,
              str(len(data["run"].get("nodes", []))))
        check("run detail has peaks", bool(data["run"]["peaks"]))

        r = await client.get("/sysmon/run/does-not-exist")
        check("unknown run -> 404", r.status == 404, str(r.status))

        r = await client.get("/sysmon/errors?limit=5")
        data = await r.json()
        check("GET /errors 200", r.status == 200)
        errs = data.get("errors", [])
        check("error record returned", len(errs) == 1 and errs[0]["node_id"] == "3",
              str(errs))
        check("error carries traceback", len(errs[0]["traceback"]) == 2)

        # ------------------------------------------------------------------
        section("Config endpoints")
        r = await client.get("/sysmon/config")
        data = await r.json()
        check("GET /config 200", r.status == 200)
        check("config key is masked in response",
              data["config"]["llm_api_key"] != "sk-mock-abcdefghijklmnopqrstuvwxyz",
              str(data["config"].get("llm_api_key")))
        check("config advertises key presence",
              data["config"].get("llm_api_key_set") is True)
        check("config exposes provider presets",
              "deepseek" in data["config"].get("provider_presets", {}))

        r = await client.post("/sysmon/config", json={"sample_interval_ms": 250,
                                                     "warn_vram_percent": 80})
        data = await r.json()
        check("POST /config 200", r.status == 200)
        check("interval applied", data["config"]["sample_interval_ms"] == 250,
              str(data["config"]["sample_interval_ms"]))
        check("threshold applied", data["config"]["warn_vram_percent"] == 80)

        r = await client.post("/sysmon/config", json={"sample_interval_ms": 5})
        data = await r.json()
        check("interval clamped to a sane minimum",
              data["config"]["sample_interval_ms"] >= 200,
              str(data["config"]["sample_interval_ms"]))

        r = await client.post("/sysmon/config",
                              data="not json",
                              headers={"Content-Type": "application/json"})
        check("malformed JSON -> 400", r.status == 400, str(r.status))

        r = await client.post("/sysmon/config", json={"llm_api_key": "***"})
        check("masked key write is ignored (200)", r.status == 200)
        check("masked write did not clobber the real key",
              config.get("llm_api_key") == "sk-mock-abcdefghijklmnopqrstuvwxyz",
              str(config.get("llm_api_key")))

        # ------------------------------------------------------------------
        section("Context preview")
        r = await client.get("/sysmon/context?prompt_id=pid-beta")
        data = await r.json()
        check("GET /context 200", r.status == 200)
        ctx = data.get("context", {})
        check("context has run metadata", ctx.get("comfyui_run", {}).get("status") == "error")
        check("context includes the error",
              ctx.get("error", {}).get("exception_type") == "RuntimeError")
        check("context omits images/prompts",
              not any(k in json.dumps(ctx).lower()
                      for k in ("base64", "data:image", "prompt_text")))

        r = await client.get("/sysmon/context")
        check("context without prompt_id uses latest run", r.status == 200, str(r.status))

        # ------------------------------------------------------------------
        section("LLM endpoints (against a local mock server)")
        r = await client.post("/sysmon/test_llm")
        data = await r.json()
        check("POST /test_llm 200", r.status == 200, str(r.status))
        check("test_llm reports ok", data.get("ok") is True, json.dumps(data)[:200])
        check("test_llm surfaces the model", data.get("model") == "mock-model")

        r = await client.post("/sysmon/analyze", json={"prompt_id": "pid-beta"})
        data = await r.json()
        check("POST /analyze 200", r.status == 200, str(r.status))
        analysis = data.get("analysis", {})
        check("analysis text returned", "结论" in analysis.get("text", ""),
              str(analysis.get("text"))[:120])
        check("analysis records usage", analysis.get("usage", {}).get("total_tokens") == 42)
        check("analysis flags the run had an error", analysis.get("had_error") is True)
        check("analysis previews what was sent",
              analysis.get("context", {}).get("error", {}).get("exception_type") == "RuntimeError")
        check("analysis persisted onto the run",
              (store.get("pid-beta").analysis or {}).get("text", "").startswith("##"))

        check("a request actually reached the model", len(_MockLLM.received) >= 2,
              str(len(_MockLLM.received)))
        body = _MockLLM.received[-1]
        check("request used the configured model", body.get("model") == "mock-model")
        check("request carried a system prompt",
              body.get("messages", [{}])[0].get("role") == "system")

        r = await client.post("/sysmon/analyze", json={"prompt_id": "pid-alpha",
                                                      "question": "显存还够吗？"})
        data = await r.json()
        check("analyze accepts a follow-up question", r.status == 200)
        check("follow-up reached the model",
              any("显存还够吗" in json.dumps(m, ensure_ascii=False)
                  for m in _MockLLM.received),
              "question not found in requests")

        # An explicitly given but unknown id must NOT silently analyse some
        # other run - that would attach advice to the wrong workflow.
        r = await client.post("/sysmon/analyze", json={"prompt_id": "nonexistent-id"})
        data = await r.json()
        check("analyze with unknown explicit id -> 400",
              r.status == 400 and not data.get("ok"), json.dumps(data, ensure_ascii=False)[:160])
        check("unknown id error message is actionable",
              "运行" in (data.get("error") or ""), str(data.get("error")))

        # Omitting the id is the documented "analyse the latest run" path.
        r = await client.post("/sysmon/analyze", json={})
        data = await r.json()
        check("analyze without id analyses the latest run",
              r.status == 200 and data.get("ok") is True,
              json.dumps(data, ensure_ascii=False)[:200])

        # ------------------------------------------------------------------
        section("AI consent gate (llm_enabled must actually stop outbound data)")
        await client.post("/sysmon/config", json={"llm_enabled": False})
        before_calls = len(_MockLLM.received)
        r = await client.post("/sysmon/analyze", json={"prompt_id": "pid-alpha"})
        data = await r.json()
        check("analyze is refused while disabled", r.status == 403, str(r.status))
        check("refusal explains how to enable it",
              "启用" in (data.get("error") or ""), str(data.get("error")))
        check("no request reached the model while disabled",
              len(_MockLLM.received) == before_calls,
              f"{before_calls} -> {len(_MockLLM.received)}")
        # Re-enable for the remaining checks.
        await client.post("/sysmon/config", json={"llm_enabled": True})
        r = await client.post("/sysmon/analyze", json={"prompt_id": "pid-alpha"})
        check("analyze works again once enabled", r.status == 200, str(r.status))

        # ------------------------------------------------------------------
        section("Follow-up thread bookkeeping is bounded")
        check("conversation store is bounded",
              api._conversations_max > 0 and api._conversations_max <= 32,
              str(api._conversations_max))
        for i in range(api._conversations_max + 6):
            api._thread(f"synthetic-{i}", create=True)
        check("oldest threads evicted, no unbounded growth",
              len(api._conversations) <= api._conversations_max,
              str(len(api._conversations)))
        check("most recent thread retained",
              f"synthetic-{api._conversations_max + 5}" in api._conversations,
              str(list(api._conversations.keys())[:3]))

        # ------------------------------------------------------------------
        section("Mutation and error handling")
        r = await client.delete("/sysmon/runs")
        check("DELETE /runs 200", r.status == 200)
        r = await client.get("/sysmon/runs")
        check("history cleared", (await r.json()).get("runs") == [])

        r = await client.post("/sysmon/analyze", json={})
        data = await r.json()
        check("analyze with no runs -> 400 + message",
              r.status == 400 and not data.get("ok"), json.dumps(data)[:160])

        r = await client.get("/sysmon/context")
        check("context with no runs -> 404", r.status == 404, str(r.status))

        r = await client.get("/sysmon/nonexistent")
        check("unknown route 404", r.status == 404, str(r.status))

        await client.close()
    finally:
        try:
            sampler.stop()
        except Exception:
            pass
        mock_llm.shutdown()
        shutil.rmtree(workdir, ignore_errors=True)


def main() -> int:
    print(f"System Monitor API tests  (python {sys.version.split()[0]})")
    asyncio.run(run_api_tests())
    print(f"\n{'=' * 46}")
    print(f"passed: {PASSED}   failed: {FAILED}")
    print("=" * 46)
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
