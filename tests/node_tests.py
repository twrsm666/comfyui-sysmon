"""Tests for the ComfyUI node surface (``SystemMonitorReport``).

The node is the one part of the plugin that runs *inside* a user's graph, so a
bug there fails their generation rather than just the panel. It must therefore
never raise, must return the declared tuple, and must report the declared
metadata ComfyUI validates against.

Importing the plugin normally starts a background boot thread and needs a live
PromptServer, so this test stubs ``server`` and neutralises ``_boot`` by
replacing it in the module namespace *before* executing the module.

    python tests/node_tests.py
"""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import types

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


def load_plugin_without_booting():
    """Import the custom-node entry point with ComfyUI stubbed out.

    The entry point starts a *daemon* thread that waits for ``PromptServer``.
    That thread is harmless here: our stub never provides an instance, so it
    eventually gives up, and being a daemon it cannot keep the test process
    alive. Swapping out ``threading`` to suppress it was tried and abandoned -
    it breaks ``asyncio``, which needs ``threading.local``.
    """
    # Minimal ``server`` stand-in exposing the attribute the boot code probes.
    server_module = types.ModuleType("server")

    class _FakePromptServer:
        instance = None

    server_module.PromptServer = _FakePromptServer  # type: ignore[attr-defined]
    sys.modules["server"] = server_module

    # Import as a real package so the entry point's relative imports resolve.
    parent = os.path.dirname(ROOT)
    if parent not in sys.path:
        sys.path.insert(0, parent)

    package_name = "sysmon_plugin_under_test"
    spec = importlib.util.spec_from_file_location(
        f"{package_name}.__init__",
        os.path.join(ROOT, "__init__.py"),
        submodule_search_locations=[ROOT],
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


def main() -> int:
    print(f"System Monitor node tests  (python {sys.version.split()[0]})")

    section("Node registration and metadata")
    plugin = load_plugin_without_booting()
    check("NODE_CLASS_MAPPINGS exists", hasattr(plugin, "NODE_CLASS_MAPPINGS"))
    check("node registered under the expected key",
          "SystemMonitorReport" in plugin.NODE_CLASS_MAPPINGS,
          str(list(plugin.NODE_CLASS_MAPPINGS.keys())))
    check("display name registered",
          "SystemMonitorReport" in plugin.NODE_DISPLAY_NAME_MAPPINGS)
    check("WEB_DIRECTORY declared", getattr(plugin, "WEB_DIRECTORY", None) == "./web",
          str(getattr(plugin, "WEB_DIRECTORY", None)))
    check("__version__ exported", isinstance(getattr(plugin, "__version__", None), str))

    node_cls = plugin.NODE_CLASS_MAPPINGS["SystemMonitorReport"]
    check("FUNCTION names a real method", hasattr(node_cls, node_cls.FUNCTION),
          node_cls.FUNCTION)
    check("CATEGORY declared", bool(getattr(node_cls, "CATEGORY", "")))
    check("RETURN_TYPES / RETURN_NAMES aligned",
          len(node_cls.RETURN_TYPES) == len(node_cls.RETURN_NAMES),
          f"{node_cls.RETURN_TYPES} vs {node_cls.RETURN_NAMES}")

    spec = node_cls.INPUT_TYPES()
    check("INPUT_TYPES has a 'required' block", "required" in spec)
    check("scope enumerated", "scope" in spec["required"])
    check("scope offers live and last_run",
          set(spec["required"]["scope"][0]) == {"live", "last_run"},
          str(spec["required"]["scope"][0]))
    optional = spec.get("optional", {})
    check("run_index is optional with sane bounds",
          optional.get("run_index", ("INT", {}))[1].get("min") == 0,
          str(optional.get("run_index")))

    section("Node behaviour without a running monitor")
    # This is what a user hits if the sampler failed to start: the node must
    # return its declared tuple rather than blowing up the graph.
    plugin._store = None
    plugin._sampler = None
    result = node_cls().report(scope="live")
    check("returns a 5-tuple", isinstance(result, tuple) and len(result) == 5,
          repr(type(result)))
    check("all declared types match",
          all(isinstance(v, (int, float)) for v in result[:4]) and isinstance(result[4], str),
          repr([type(v).__name__ for v in result]))
    check("explains the monitor is not running",
          "not running" in result[4].lower(), repr(result[4]))
    check("loopback still works with no monitor",
          len(node_cls().report(scope="last_run")) == 5)

    section("Node behaviour with a live monitor")
    workdir = tempfile.mkdtemp(prefix="sysmon_node_")
    try:
        from sysmon.config import Config
        from sysmon.metrics import MetricsSampler
        from sysmon.store import RunStore

        config = Config(os.path.join(workdir, "cfg.json"))
        config.update({"sample_interval_ms": 200, "sample_interval_active_ms": 200,
                       "history_size": 60})
        sampler = MetricsSampler(config)
        sampler.start()
        store = RunStore(config, sampler, os.path.join(workdir, "logs"))
        plugin._sampler = sampler
        plugin._store = store

        live = node_cls().report(scope="live")
        check("live scope returns numbers", all(isinstance(v, (int, float)) for v in live[:4]))
        check("live report mentions CPU", "CPU" in live[4], repr(live[4][:80]))
        check("live report mentions GPU or is explicit about absence",
              ("GPU" in live[4]) or ("No sample" in live[4]), repr(live[4][:80]))
        check("total VRAM is a plausible number",
              live[1] >= 0, repr(live[1]))
        check("percentages within range",
              all(0 <= v <= 100 for v in (live[0], live[2], live[3])),
              repr(live[:4]))

        # A finished run, then the last_run scope.
        store.begin_run("pid-node", {"1": {"class_type": "KSampler"}},
                        {"extra_pnginfo": {"workflow": {"name": "node-test",
                         "nodes": [{"id": 1, "title": "采样"}]}}})
        store.note_node_start("1")
        time.sleep(0.3)
        store.note_node_end("1")
        store.end_run("success", "pid-node")

        last = node_cls().report(scope="last_run")
        check("last_run returns a 5-tuple", len(last) == 5)
        check("last_run text describes the run",
              "node-test" in last[4] and "success" in last[4], repr(last[4]))
        check("last_run reports a peak", "peak" in last[4], repr(last[4][:120]))
        check("vram peak is a real number", last[1] >= 0, repr(last[1]))

        # run_index walk-back must not raise when it exceeds history.
        far = node_cls().report(scope="last_run", run_index=99)
        check("out-of-range run_index degrades safely", len(far) == 5, repr(far[4][:60]))

        # IS_CHANGED must force re-evaluation (this node reports live data).
        changed = node_cls.IS_CHANGED()
        check("IS_CHANGED forces re-run", changed != changed,  # NaN != NaN
              repr(changed))

        # A store that raises must not escape as an exception.
        class Exploding:
            def latest(self):
                raise RuntimeError("boom")

            def history(self, n):
                raise RuntimeError("boom")

        plugin._sampler = Exploding()
        plugin._store = Exploding()
        safe = node_cls().report(scope="live")
        check("internal failure is caught, not raised",
              len(safe) == 5 and "error" in safe[4].lower(), repr(safe[4][:90]))

        sampler.stop()
    finally:
        shutil.rmtree(workdir, ignore_errors=True)

    print(f"\n{'=' * 46}")
    print(f"passed: {PASSED}   failed: {FAILED}")
    print("=" * 46)
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
