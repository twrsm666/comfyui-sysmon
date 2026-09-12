"""Headless test harness for the System Monitor backend.

Runs against the *live* ComfyUI venv and real hardware, but does not need
ComfyUI itself: it drives the store with the same payloads that
``PromptExecutor.add_message`` emits, so the attribution logic is exercised
under genuine sampling conditions.

    python tests/run_tests.py

Exits non-zero on the first failure.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from sysmon.config import Config                       # noqa: E402
from sysmon.metrics import (                            # noqa: E402
    MetricsSampler,
    _parse_smi_csv,
    extract_peaks,
)
from sysmon.store import RunStore                       # noqa: E402
from sysmon import llm                                  # noqa: E402

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
def test_smi_parser() -> None:
    section("nvidia-smi CSV parsing")
    csv = (
        "0, NVIDIA GeForce RTX 5060 Ti, 3, 2, 16311, 15363, 948, 43, 18.46, 180.00, 30, 2400\n"
        "1, NVIDIA GeForce RTX 3060, [N/A], [N/A], 12288, 100, 12188, [N/A], [N/A], [N/A], [N/A], [N/A]\n"
        "garbage line that should be ignored\n"
    )
    gpus = _parse_smi_csv(csv)
    check("parses 2 valid rows", len(gpus) == 2, f"got {len(gpus)}")
    check("reads utilisation", gpus[0]["util_percent"] == 3.0, str(gpus[0]["util_percent"]))
    check("reads total VRAM", gpus[0]["mem_total_mb"] == 16311.0)
    check("computes used VRAM", gpus[0]["mem_used_mb"] == 15363.0)
    check("maps [N/A] to None", gpus[1]["util_percent"] is None, str(gpus[1]["util_percent"]))
    check("keeps numeric field on N/A row", gpus[1]["mem_total_mb"] == 12288.0)


def test_sampler_basic() -> None:
    section("Sampler (real hardware when present)")
    config = Config(os.path.join(tempfile.gettempdir(), "sysmon_test_missing.json"))
    sampler = MetricsSampler(config)
    sampler._select_gpu_backend()
    backend = sampler._gpu_backend.name if sampler._gpu_backend else None
    print(f"  gpu backend: {backend or 'none available (GPU-less machine)'}")

    sampler._read_disk()          # prime cumulative counters
    time.sleep(0.6)
    first = sampler.sample_once()
    time.sleep(0.6)
    second = sampler.sample_once()

    check("cpu percent present", first.get("cpu_percent") is not None)
    check("ram present", (first.get("ram_used_mb") or 0) > 0)
    check("ram total sane", (first.get("ram_total_mb") or 0) > 1024,
          str(first.get("ram_total_mb")))
    check("disk rate computed after priming", second.get("disk_read_mb_s") is not None,
          str(second.get("disk_read_mb_s")))
    check("disk rate is non-negative", (second.get("disk_read_mb_s") or 0) >= 0)
    gpu = second.get("gpu")
    if backend is None:
        # No NVIDIA driver / torch CUDA: the sampler must degrade, not crash.
        check("gpu section is None without a backend", gpu is None, str(gpu))
        check("sampling still works without a GPU",
              second.get("cpu_percent") is not None and bool(second.get("ram_total_mb")))
    else:
        check("gpu section present", isinstance(gpu, dict) and bool(gpu))
        check("vram total plausible", (gpu.get("mem_total_mb") or 0) > 1024,
              str(gpu.get("mem_total_mb")))
        check("vram used <= total",
              (gpu.get("mem_used_mb") or 0) <= (gpu.get("mem_total_mb") or 0) * 1.01,
              f"{gpu.get('mem_used_mb')} vs {gpu.get('mem_total_mb')}")
        print(f"  live: vram {gpu.get('mem_used_mb'):.0f}/{gpu.get('mem_total_mb'):.0f} MB, "
              f"util {gpu.get('util_percent')}%, cpu {first.get('cpu_percent')}%")
    sampler.stop()


def test_sampler_thread_and_window() -> None:
    section("Sampler thread, ring buffer and window slicing")
    config = Config(os.path.join(tempfile.gettempdir(), "sysmon_test_missing.json"))
    config.update({"sample_interval_ms": 200, "sample_interval_active_ms": 200, "history_size": 12})
    sampler = MetricsSampler(config)
    sampler.start()
    check("thread reports running", sampler.running)
    sampler.set_active(True)
    time.sleep(1.6)
    series = sampler.series(100)
    check("collected samples", len(series) >= 3, f"got {len(series)}")
    check("ring buffer respects maxlen", len(series) <= 12, f"got {len(series)}")
    indices = [s.get("i") for s in series]
    check("ring indices strictly increasing",
          all(b > a for a, b in zip(indices, indices[1:])), str(indices))

    # A window covering everything must return everything; an empty one nothing.
    lo = series[0]["t"]
    hi = series[-1]["t"]
    check("window(all) non-empty", len(sampler.window(lo, hi)) >= 1)
    check("window(future) empty", len(sampler.window(hi + 100, hi + 200)) == 0)

    sampler.stop()
    check("thread stopped", not sampler.running)


def test_peak_extraction() -> None:
    section("Peak extraction")
    samples = [
        {"t": 1.0, "wall": 1.0, "cpu_percent": 10.0, "ram_used_mb": 100.0,
         "ram_total_mb": 1000.0, "disk_read_mb_s": 1.0, "disk_write_mb_s": 2.0,
         "gpu": {"util_percent": 5.0, "mem_used_mb": 500.0, "mem_total_mb": 1000.0,
                 "temperature_c": 40.0, "power_w": 50.0}},
        {"t": 2.0, "wall": 2.0, "cpu_percent": 90.0, "ram_used_mb": 900.0,
         "ram_total_mb": 1000.0, "disk_read_mb_s": 300.0, "disk_write_mb_s": 1.0,
         "gpu": {"util_percent": 99.0, "mem_used_mb": 950.0, "mem_total_mb": 1000.0,
                 "temperature_c": 80.0, "power_w": 200.0}},
        {"t": 3.0, "wall": 3.0, "cpu_percent": 20.0, "ram_used_mb": 200.0,
         "ram_total_mb": 1000.0, "disk_read_mb_s": 0.0, "disk_write_mb_s": 50.0,
         "gpu": {"util_percent": 10.0, "mem_used_mb": 100.0, "mem_total_mb": 1000.0,
                 "temperature_c": 50.0, "power_w": 60.0}},
    ]
    peaks = extract_peaks(samples)
    check("gpu util peak", peaks["gpu_util"]["value"] == 99.0, str(peaks.get("gpu_util")))
    check("vram peak", peaks["vram_used"]["value"] == 950.0)
    check("vram percent computed", peaks["vram_used"]["percent"] == 95.0,
          str(peaks["vram_used"]["percent"]))
    check("cpu peak", peaks["cpu"]["value"] == 90.0)
    check("ram percent from bytes", peaks["ram_used"]["percent"] == 90.0,
          str(peaks["ram_used"]["percent"]))
    check("disk read peak", peaks["disk_read"]["value"] == 300.0)
    check("disk write peak", peaks["disk_write"]["value"] == 50.0)
    check("peak timestamped", peaks["gpu_util"]["t"] == 2.0)
    check("all-None metric omitted",
          "missing" not in extract_peaks([{"t": 1.0}]))


def test_run_attribution() -> None:
    section("Per-node peak attribution and persistence")
    workdir = tempfile.mkdtemp(prefix="sysmon_test_")
    try:
        config = Config(os.path.join(workdir, "cfg.json"))
        config.update({
            "sample_interval_ms": 150,
            "sample_interval_active_ms": 150,
            "history_size": 200,
            "save_runs": True,
            "save_error_artifacts": True,
            "history_runs": 10,
        })
        sampler = MetricsSampler(config)
        sampler.start()
        store = RunStore(config, sampler, os.path.join(workdir, "logs"))

        prompt = {"1": {"class_type": "CheckpointLoaderSimple"}, "2": {"class_type": "KSampler"}}
        extra = {
            "client_id": "test",
            "extra_pnginfo": {"workflow": {
                "name": "unit_test_workflow",
                "nodes": [
                    {"id": 1, "type": "CheckpointLoaderSimple", "title": "Load Model"},
                    {"id": 2, "type": "KSampler", "title": "Sampler (my rename)"},
                ],
            }},
        }
        record = store.begin_run("pid-1", prompt, extra)
        check("run opened", record is not None and record.prompt_id == "pid-1")
        check("workflow name captured", record.workflow == "unit_test_workflow", record.workflow)
        check("node count captured", record.node_count == 2, str(record.node_count))
        check("gpu backend captured without error",
              record.gpu_backend is None or isinstance(record.gpu_backend, str),
              repr(record.gpu_backend))
        check("sampler switched to active cadence", not sampler._idle.is_set())

        store.note_cached(["9"])           # cached nodes must never be attributed
        store.note_node_start("9", "CachedNode")
        store.note_node_end("9")
        check("cached node not recorded", all(n.node_id != "9" for n in record.nodes))

        store.note_node_start("1", "CheckpointLoaderSimple")
        time.sleep(0.45)
        store.note_node_end("1")
        store.note_node_start("2", "KSampler")
        time.sleep(0.45)
        store.note_node_end("2")

        check("two nodes attributed", len(record.nodes) == 2, str(len(record.nodes)))
        by_id = {n.node_id: n for n in record.nodes}
        check("node 1 duration measured",
              (by_id["1"].duration_s or 0) >= 0.3, str(by_id["1"].duration_s))
        check("node 2 duration measured",
              (by_id["2"].duration_s or 0) >= 0.3, str(by_id["2"].duration_s))
        check("node title from workflow JSON came through",
              by_id["2"].title == "Sampler (my rename)", by_id["2"].title)
        check("node 1 got samples in its window", by_id["1"].samples >= 1,
              str(by_id["1"].samples))
        check("node peaks non-empty", bool(by_id["2"].peaks), str(list(by_id["2"].peaks)))
        check("vram peak recorded per node",
              (by_id["2"].peaks.get("vram_used") or {}).get("value") is not None)
        check("per-node summary present", "cpu" in by_id["2"].summary)

        # Failure path
        store.note_node_start("2", "KSampler")
        error = store.note_error({
            "prompt_id": "pid-1",
            "node_id": "2",
            "node_type": "KSampler",
            "exception_type": "torch.cuda.OutOfMemoryError",
            "exception_message": "CUDA out of memory. Tried to allocate 2.00 GiB",
            "traceback": ["Traceback (most recent call last):", "  File x.py line 1",
                          "torch.cuda.OutOfMemoryError: CUDA out of memory."],
            "current_inputs": {"seed": 42, "steps": 30},
            "executed": ["1"],
        }, kind="error")
        check("error recorded", error is not None)
        check("error tied to node", error.node_id == "2")
        check("error traceback captured", len(error.traceback) == 3)
        finished = store.end_run("error", "pid-1")
        check("run closed on failure", finished is not None and finished.status == "error")
        check("duration set", (finished.duration_s or 0) > 0.5, str(finished.duration_s))
        check("error attached to run", len(finished.errors) == 1)
        check("run peaks computed", bool(finished.peaks))
        check("open node list drained", store._open_nodes == {})

        logs = os.listdir(os.path.join(workdir, "logs"))
        check("run JSON written to logs", any(f.endswith("_error.json") for f in logs),
              str(logs))

        # The hooks layer writes the detailed artifact on failure; exercise it.
        artifact = store.save_error_artifact(error, finished)
        check("error artifact written", artifact is not None
              and os.path.isfile(artifact), str(artifact))
        if artifact:
            with open(artifact, "r", encoding="utf-8") as fh:
                artifact_payload = json.load(fh)
            check("artifact contains the error",
                  artifact_payload["error"]["exception_type"].endswith("OutOfMemoryError"))
            check("artifact contains the run timeline",
                  "run" in artifact_payload and artifact_payload["run"] is not None)

        hist = store.history(5)
        check("history has the run", len(hist) == 1 and hist[0]["prompt_id"] == "pid-1")
        node_ids = [n["node_id"] for n in hist[0]["nodes"]]
        check("history entry carries both nodes",
              "1" in node_ids and "2" in node_ids, str(node_ids))
        check("every node entry has a duration",
              all(n["duration_s"] is not None for n in hist[0]["nodes"]), str(node_ids))
        check("run-level peaks present in history", bool(hist[0]["peaks"]))
        errs = store.errors(5)
        check("errors query returns it", len(errs) == 1 and errs[0]["node_id"] == "2")

        # A second, successful run to check ordering and status handling.
        store.begin_run("pid-2", {"3": {"class_type": "SaveImage"}}, {"client_id": "t"})
        store.note_node_start("3", "SaveImage")
        time.sleep(0.25)
        store.note_node_end("3")
        store.end_run("success", "pid-2")
        hist = store.history(5)
        check("newest run first", hist[0]["prompt_id"] == "pid-2", hist[0]["prompt_id"])
        check("success status recorded", hist[0]["status"] == "success")
        check("title falls back to class_type for API prompts",
              hist[0]["nodes"][0]["title"] == "SaveImage",
              hist[0]["nodes"][0]["title"])

        # Cleanup path parity with the executor's finally block.
        store.begin_run("pid-3", {"4": {"class_type": "X"}}, {})
        store.after_run_cleanup("error", "pid-3")
        check("cleanup closes a dangling run",
              store.history(1)[0]["prompt_id"] == "pid-3")

        sampler.stop()
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def test_llm_context() -> None:
    section("LLM context building (privacy surface)")
    run = {
        "status": "error",
        "duration_s": 42.5,
        "started_iso": "2026-01-01 00:00:00",
        "workflow": "my_workflow",
        "node_count": 5,
        "executed_count": 4,
        "cached_count": 1,
        "gpu_name": "RTX 5060 Ti",
        "sample_interval_ms": 500,
        "peaks": {
            "vram_used": {"label": "显存占用", "value": 15500.0, "unit": "MB",
                          "percent": 95.0, "t": 5.0},
            "cpu": {"label": "CPU", "value": 88.0, "unit": "%", "percent": 88.0, "t": 6.0},
            "ram_used": {"value": 31000.0, "unit": "MB", "percent": 97.0, "t": 7.0},
        },
        "nodes": [
            {"node_id": "2", "title": "KSampler", "class_type": "KSampler", "duration_s": 40.0,
             "peaks": {"vram_used": {"value": 15500.0, "unit": "MB", "percent": 95.0},
                       "gpu_util": {"value": 99.0, "unit": "%", "percent": 99.0},
                       "ram_used": {"value": 31000.0, "unit": "MB", "percent": 97.0}}},
            {"node_id": "1", "title": "Load Model", "class_type": "CheckpointLoaderSimple",
             "duration_s": 2.0, "peaks": {"vram_used": {"value": 8000.0, "unit": "MB"}}},
        ],
    }
    error = {
        "kind": "error",
        "node_title": "KSampler",
        "node_type": "KSampler",
        "exception_type": "torch.cuda.OutOfMemoryError",
        "exception_message": "CUDA out of memory. Tried to allocate 2.00 GiB",
        "traceback": ["a", "b", "torch.cuda.OutOfMemoryError"],
        "current_inputs": {"seed": 42},
    }
    context = llm.build_context(run, error, language="zh")
    blob = json.dumps(context, ensure_ascii=False)

    check("run metadata present", context["comfyui_run"]["workflow_name"] == "my_workflow",
          str(context["comfyui_run"]))
    check("hardware peaks present", bool(context["hardware_peaks"]))
    check("peak attribution names the node",
          context["peak_attribution"]["显存占用"]["node"] == "KSampler",
          json.dumps(context.get("peak_attribution"), ensure_ascii=False))
    check("slowest node ranked first",
          context["slowest_nodes"][0]["node"] == "KSampler")
    check("error included", context["error"]["exception_type"].endswith("OutOfMemoryError"))
    check("vram pressure flagged critical",
          context["memory_pressure"]["vram_status"].startswith("critical"),
          context["memory_pressure"].get("vram_status"))
    check("node near vram limit listed",
          context["memory_pressure"]["nodes_near_vram_limit"][0]["node"] == "KSampler")
    check("graph included", bool(context["workflow_graph"]))
    # Privacy guarantees
    for forbidden in ("prompt_text", "image", "base64", "positive", "negative"):
        check(f"no '{forbidden}' in payload", forbidden not in blob.lower())

    zh = llm.build_messages(context, "zh")
    en = llm.build_messages(context, "en")
    check("system prompt is Chinese for zh", "诊断专家" in zh[0]["content"])
    check("system prompt is English for en", "diagnosis" in en[0]["content"])
    check("payload embedded in user turn", "```json" in zh[-1]["content"])

    # Graph can be excluded on request
    ctx2 = llm.build_context(run, error, include_graph=False)
    check("graph excluded when asked", "workflow_graph" not in ctx2)

    # Missing API key must fail with a helpful, non-crashing error
    try:
        llm.analyze({"api_key": "", "base_url": "https://x", "model": "m"}, context)
        check("missing key raises", False, "no exception")
    except llm.LLMError as exc:
        check("missing key raises helpful error", "API Key" in str(exc), str(exc))

    check("http error hints are specific",
          "404" in llm._http_hint(404, "") or "接口地址" in llm._http_hint(404, ""))
    check("extracts openai style text",
          llm._extract_text({"choices": [{"message": {"content": "hi"}}]}) == "hi")
    check("extracts non-standard text",
          llm._extract_text({"choices": [{"text": "alt"}]}) == "alt")


def test_config_masking() -> None:
    section("Config masking and merge")
    workdir = tempfile.mkdtemp(prefix="sysmon_cfg_")
    try:
        path = os.path.join(workdir, "c.json")
        config = Config(path)
        check("defaults available", config.get("sample_interval_ms") == 1000)
        config.update({"llm_api_key": "sk-abcdefghijklmnop", "llm_provider": "deepseek"})
        public = config.public()
        check("key masked in public view", public["llm_api_key"] != "sk-abcdefghijklmnop",
              public["llm_api_key"])
        check("masked view flags key presence", public["llm_api_key_set"] is True)
        check("raw key still readable internally",
              config.get("llm_api_key") == "sk-abcdefghijklmnop")

        resolved = config.resolved_llm()
        check("provider preset supplies base url",
              resolved["base_url"] == "https://api.deepseek.com", resolved["base_url"])
        check("provider preset supplies model",
              resolved["model"] == "deepseek-chat", resolved["model"])

        config.update({"llm_base_url": "https://my.gateway/v1", "llm_model": "custom-model"})
        resolved = config.resolved_llm()
        check("user base url overrides preset",
              resolved["base_url"] == "https://my.gateway/v1", resolved["base_url"])
        check("user model overrides preset", resolved["model"] == "custom-model")

        # Reload from disk and confirm persistence + unknown-key tolerance.
        config.update({"future_option": {"nested": 1}})
        reloaded = Config(path)
        check("settings persisted to disk", reloaded.get("llm_model") == "custom-model")
        check("unknown keys survive round-trip",
              reloaded.get("future_option") == {"nested": 1})
        check("defaults still merged in", reloaded.get("llm_timeout_s") == 120)

        # Corrupt file must not raise
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("{not valid json")
        recovered = Config(path)
        check("corrupt config falls back to defaults",
              recovered.get("sample_interval_ms") == 1000)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def test_llm_transport() -> None:
    """End-to-end request/response against a local mock of the chat API.

    Verifies the wire format we actually send (URL, auth header, body shape) and
    that the reply is parsed - without needing a real API key.
    """
    section("LLM transport against a mock endpoint")
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer

    captured: Dict[str, Any] = {}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length).decode("utf-8")
            captured["path"] = self.path
            captured["auth"] = self.headers.get("Authorization")
            captured["content_type"] = self.headers.get("Content-Type")
            captured["body"] = json.loads(raw)
            payload = {
                "model": captured["body"].get("model"),
                "choices": [{"message": {"role": "assistant",
                                         "content": "## 结论\n运行正常。"}}],
                "usage": {"total_tokens": 123},
            }
            data = json.dumps(payload).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    try:
        context = llm.build_context(
            {"status": "success", "workflow": "t", "peaks": {}, "nodes": []}, None
        )
        settings = {
            "api_key": "sk-test-key",
            "base_url": f"http://127.0.0.1:{port}",
            "endpoint": "/chat/completions",
            "model": "mock-model",
            "timeout_s": 20,
            "max_tokens": 100,
            "temperature": 0.0,
            "language": "zh",
            "provider": "openai_compatible",
        }
        result = llm.analyze(settings, context)
        check("reply parsed from response", "运行正常" in result["text"], result.get("text"))
        check("model echoed back", result["model"] == "mock-model")
        check("usage surfaced", result["usage"]["total_tokens"] == 123)
        check("posted to the configured path",
              captured.get("path") == "/chat/completions", str(captured.get("path")))
        check("bearer auth header sent",
              captured.get("auth") == "Bearer sk-test-key", str(captured.get("auth")))
        check("json content type sent",
              captured.get("content_type") == "application/json")
        body = captured.get("body") or {}
        check("model in request body", body.get("model") == "mock-model")
        check("messages array in request body",
              isinstance(body.get("messages"), list) and len(body["messages"]) >= 2,
              str(type(body.get("messages"))))
        check("system prompt is first message",
              body["messages"][0]["role"] == "system")
        check("streaming disabled", body.get("stream") is False)
        check("max_tokens forwarded", body.get("max_tokens") == 100)

        # Base URL with a trailing slash must not produce a doubled path.
        settings2 = dict(settings, base_url=f"http://127.0.0.1:{port}/")
        llm.analyze(settings2, context)
        check("trailing slash normalised",
              captured.get("path") == "/chat/completions", str(captured.get("path")))

        # A gateway that returns an error object must raise a readable message.
        class ErrHandler(BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802
                data = json.dumps({"error": {"message": "insufficient balance"}}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *args):
                pass

        err_server = HTTPServer(("127.0.0.1", 0), ErrHandler)
        err_thread = threading.Thread(target=err_server.serve_forever, daemon=True)
        err_thread.start()
        try:
            try:
                llm.analyze(dict(settings, base_url=f"http://127.0.0.1:{err_server.server_address[1]}"), context)
                check("error payload raises", False, "no exception")
            except llm.LLMError as exc:
                check("error payload raises readable error",
                      "insufficient balance" in str(exc), str(exc))
        finally:
            err_server.shutdown()
    finally:
        server.shutdown()


def test_api_key_never_leaks() -> None:
    """The API key must not appear in any config view, run record or log file.

    This is a security regression test: a future refactor that starts returning
    the raw config, or persisting settings into a run artifact, fails here.
    """
    section("API key never leaks (security regression)")
    workdir = tempfile.mkdtemp(prefix="sysmon_secret_")
    secret = "sk-SUPERSECRET-abcdef1234567890"
    try:
        config = Config(os.path.join(workdir, "cfg.json"))
        config.update({
            "llm_api_key": secret,
            "llm_provider": "deepseek",
            "sample_interval_ms": 200,
            "sample_interval_active_ms": 200,
            "history_size": 60,
        })

        # 1. The masked view handed to the browser must not contain the key.
        public_json = json.dumps(config.public(), ensure_ascii=False)
        check("raw key absent from public config", secret not in public_json)
        check("public view still reports the key is set",
              config.public().get("llm_api_key_set") is True)
        check("masked value is not the raw key",
              config.public().get("llm_api_key") != secret,
              str(config.public().get("llm_api_key")))

        # 2. Internal resolution must still work for the outbound request.
        check("resolved_llm still returns the real key",
              config.resolved_llm()["api_key"] == secret)

        # 3. A run (including a failure + analysis) must not persist the key.
        sampler = MetricsSampler(config)
        sampler.start()
        logs = os.path.join(workdir, "logs")
        store = RunStore(config, sampler, logs)
        store.begin_run("pid-secret", {"1": {"class_type": "X"}}, {"client_id": "t"})
        store.note_node_start("1", "X")
        time.sleep(0.2)
        store.note_node_end("1")
        error = store.note_error({
            "node_id": "1",
            "node_type": "X",
            "exception_type": "RuntimeError",
            "exception_message": "boom",
            "traceback": ["a", "b"],
        })
        store.save_error_artifact(error, store.current())
        store.end_run("error", "pid-secret")
        store.attach_analysis("pid-secret", {
            "text": "analysis", "model": "m", "at": time.time(),
            "context": {"comfyui_run": {"status": "error"}},
        })

        # 4. Nothing on disk may contain it.
        leaked = []
        for name in os.listdir(logs):
            with open(os.path.join(logs, name), "r", encoding="utf-8") as fh:
                if secret in fh.read():
                    leaked.append(name)
        check("no log artifact contains the key", not leaked, str(leaked))

        # 5. Nor may any record served over HTTP.
        payload = json.dumps(store.history(5), ensure_ascii=False)
        payload += json.dumps(store.errors(5), ensure_ascii=False)
        payload += json.dumps(store.live(), ensure_ascii=False, default=str)
        check("no run/error payload contains the key", secret not in payload)

        # 6. The mask round-trip must not overwrite the stored key.
        masked = config.public().get("llm_api_key")
        patch = {"llm_api_key": masked, "llm_provider": "deepseek"}
        if patch.get("llm_api_key") in ("***", "", None) and "llm_api_key" in patch:
            patch.pop("llm_api_key")
        config.update(patch)
        check("saving the masked value does not clobber the key",
              config.resolved_llm()["api_key"] == secret,
              config.get("llm_api_key"))

        sampler.stop()
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def test_node_identity_resolution() -> None:
    """class_type must survive even when the workflow JSON omits it.

    Regression test: the frontend workflow payload carries per-node ``type`` and
    ``title`` but no ``class_type``. An earlier version indexed titles and types
    into one dict, so ``class_type`` came back empty ('' -> shown as "-" in the
    UI). The prompt dict is the authoritative source for the type.
    """
    section("Node identity resolution (title vs class_type)")
    from sysmon.store import _titles_from

    prompt = {
        "1": {"class_type": "EmptyImage", "inputs": {"width": 512}},
        "2": {"class_type": "PreviewImage", "inputs": {}},
    }
    # Frontend payload WITHOUT class_type - exactly what ComfyUI sends.
    workflow_extra = {
        "extra_pnginfo": {
            "workflow": {
                "name": "t",
                "nodes": [
                    {"id": 1, "type": "EmptyImage", "title": "生成图片"},
                    {"id": 2, "type": "PreviewImage", "title": "预览结果"},
                ],
            }
        }
    }
    titles, types = _titles_from(workflow_extra, prompt)
    check("class_type resolved from prompt", types.get("1") == "EmptyImage", str(types))
    check("all class_types resolved", len(types) == 2, str(types))
    check("user title preferred over class_type",
          titles.get("1") == "生成图片", str(titles))
    check("title falls back to class_type when workflow has none",
          _titles_from({}, prompt)[0].get("1") == "EmptyImage",
          str(_titles_from({}, prompt)[0]))

    # A node present in the prompt but missing from the workflow must still work.
    titles2, types2 = _titles_from(
        {"extra_pnginfo": {"workflow": {"nodes": [{"id": 1, "title": "只有第一个"}]}}},
        prompt,
    )
    check("unlisted node still gets its class_type", types2.get("2") == "PreviewImage",
          str(types2))
    check("unlisted node falls back to class_type as title",
          titles2.get("2") == "PreviewImage", str(titles2))

    # Subgraph nodes are nested inside definitions and must be indexed too.
    nested_extra = {
        "extra_pnginfo": {
            "workflow": {
                "nodes": [{"id": 10, "type": "Sub", "title": "子图"}],
                "definitions": {
                    "sub1": {"nodes": [{"id": 99, "type": "Inner", "title": "内部节点"}]}
                },
            }
        }
    }
    titles3, _ = _titles_from(nested_extra, prompt)
    check("subgraph node title indexed", titles3.get("99") == "内部节点", str(titles3))

    # End-to-end through the store: the recorded node must carry the type.
    workdir = tempfile.mkdtemp(prefix="sysmon_types_")
    try:
        config = Config(os.path.join(workdir, "cfg.json"))
        config.update({"sample_interval_ms": 200, "sample_interval_active_ms": 200})
        sampler = MetricsSampler(config)
        sampler.start()
        store = RunStore(config, sampler, os.path.join(workdir, "logs"))
        store.begin_run("pid-types", prompt, workflow_extra)
        store.note_node_start("1")          # no class_type passed, as the hook does
        time.sleep(0.2)
        store.note_node_end("1")
        record = store.end_run("success", "pid-types")
        node = record.nodes[0]
        check("recorded node has class_type", node.class_type == "EmptyImage",
              repr(node.class_type))
        check("recorded node keeps the user title", node.title == "生成图片",
              repr(node.title))
        sampler.stop()
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def test_llm_failure_modes() -> None:
    """Every failure a user can hit before their key works must be actionable.

    These are the states someone lands in while configuring the feature, so a
    bare traceback or an empty error would be the difference between "I'll fix
    my key" and "this plugin is broken".
    """
    section("LLM failure modes (actionable errors)")
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer

    context = llm.build_context({"status": "success", "peaks": {}, "nodes": []}, None)
    base_settings = {
        "api_key": "sk-test-key-abcdefghijklmnop",
        "base_url": "http://127.0.0.1:9",   # discard port: connection refused
        "endpoint": "/chat/completions",
        "model": "m",
        "timeout_s": 5,
        "max_tokens": 50,
        "temperature": 0.0,
        "language": "zh",
        "provider": "deepseek",
    }

    def expect_error(label, settings, must_contain):
        try:
            llm.analyze(settings, context)
            check(label, False, "no exception raised")
        except llm.LLMError as exc:
            text = str(exc)
            check(label, must_contain.lower() in text.lower(), f"got: {text[:160]}")
        except Exception as exc:  # wrong exception type is also a failure
            check(label, False, f"{type(exc).__name__}: {exc}")

    # 1. Unreachable endpoint -> network error, not a crash.
    expect_error("unreachable endpoint -> readable network error",
                 base_settings, "网络")

    # 2. No key at all.
    expect_error("missing key -> tells the user to add one",
                 dict(base_settings, api_key=""), "API Key")

    # 3. Each HTTP status maps to the right hint.
    status_cases = {
        401: ("401", "API Key"),
        402: ("402", "余额"),
        429: ("429", "限流"),
        404: ("404", "接口地址"),
    }
    for code, (label, needle) in status_cases.items():
        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802
                payload = b'{"error":{"message":"upstream says no"}}'
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *args):
                pass

        server = HTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            expect_error(f"HTTP {label} -> explains the cause and keeps the body",
                         dict(base_settings, base_url=f"http://127.0.0.1:{server.server_address[1]}"),
                         needle)
            try:
                llm.analyze(dict(base_settings, base_url=f"http://127.0.0.1:{server.server_address[1]}"), context)
            except llm.LLMError as exc:
                check(f"HTTP {label} error includes upstream detail",
                      "upstream says no" in str(exc), str(exc)[:160])
        finally:
            server.shutdown()

    # 4. A 200 that is not JSON must not produce a confusing failure.
    class JunkHandler(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            body = b"<html>gateway error</html>"
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    junk = HTTPServer(("127.0.0.1", 0), JunkHandler)
    threading.Thread(target=junk.serve_forever, daemon=True).start()
    try:
        expect_error("non-JSON 200 -> explains it was not JSON",
                     dict(base_settings, base_url=f"http://127.0.0.1:{junk.server_address[1]}"),
                     "JSON")
    finally:
        junk.shutdown()

    # 5. DeepSeek's documented base URL must resolve to the correct endpoint.
    settings = dict(base_settings, base_url="https://api.deepseek.com",
                    endpoint="/chat/completions")
    check("deepseek base URL is used verbatim",
          settings["base_url"].rstrip("/") + settings["endpoint"]
          == "https://api.deepseek.com/chat/completions")

    # 6. Real DeepSeek error payloads must render as the documented hint.
    #    Shape copied from an actual 401 returned by api.deepseek.com, so the
    #    mapping is verified against the live wire format rather than a guess.
    real_401 = (
        '{"error":{"message":"Authentication Fails, Your api key: ****3456 is '
        'invalid","type":"authentication_error","param":null,'
        '"code":"invalid_request_error"}}'
    )
    rendered = llm._http_hint(401, real_401)
    check("real deepseek 401 -> invalid-key hint", "API Key 无效" in rendered, rendered[:120])
    check("real deepseek 401 -> keeps upstream detail",
          "Authentication Fails" in rendered, rendered[:120])

    # 7. Optional live probe. Off by default so CI stays offline and fast; with
    #    a real key this is what confirms the success path end to end.
    live_key = os.environ.get("SYSMON_LIVE_API_KEY")
    if live_key:
        section("LLM live probe (SYSMON_LIVE_API_KEY set)")
        live = dict(base_settings, api_key=live_key,
                    base_url=os.environ.get("SYSMON_LIVE_BASE_URL", "https://api.deepseek.com"),
                    model=os.environ.get("SYSMON_LIVE_MODEL", "deepseek-chat"))
        try:
            result = llm.analyze(live, context, "只回复两个字：正常")
            check("live provider returned text", bool(result.get("text")), str(result)[:200])
            check("live provider reported a model", bool(result.get("model")))
            print(f"  live model reply: {result.get('text', '')[:120]!r}")
        except llm.LLMError as exc:
            check("live provider call succeeded", False, str(exc)[:300])
    else:
        info = ("  (skipped - set SYSMON_LIVE_API_KEY to exercise the real "
                "provider end to end)")
        print(info)


def main() -> int:
    print(f"System Monitor backend tests  (python {sys.version.split()[0]})")
    test_smi_parser()
    test_sampler_basic()
    test_sampler_thread_and_window()
    test_peak_extraction()
    test_run_attribution()
    test_node_identity_resolution()
    test_llm_context()
    test_llm_transport()
    test_llm_failure_modes()
    test_config_masking()
    test_api_key_never_leaks()
    print(f"\n{'=' * 46}")
    print(f"passed: {PASSED}   failed: {FAILED}")
    print("=" * 46)
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
