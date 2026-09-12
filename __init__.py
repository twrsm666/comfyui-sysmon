"""ComfyUI System Monitor.

Live GPU / VRAM / CPU / RAM / disk monitoring for ComfyUI, with per-node peak
attribution, run duration and error logging, and optional AI analysis of a run
through DeepSeek or any OpenAI-compatible endpoint.

This module is the custom-node entry point.  It must stay import-cheap and
must never raise: a broken monitoring plugin should not stop ComfyUI booting.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from typing import Any, Dict, Tuple

from .sysmon import __version__
from .sysmon.api import MonitorAPI
from .sysmon.config import Config
from .sysmon.hooks import ComfyHooks
from .sysmon.metrics import MetricsSampler
from .sysmon.store import RunStore

logger = logging.getLogger("sysmon")

PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_DIR = os.path.join(PLUGIN_DIR, "logs")
CONFIG_PATH = os.path.join(PLUGIN_DIR, "sysmon_config.json")

WEB_DIRECTORY = "./web"

_config: Config | None = None
_sampler: MetricsSampler | None = None
_store: RunStore | None = None
_api: MonitorAPI | None = None
_hooks: ComfyHooks | None = None
_lock = threading.Lock()
_started = False


def _boot() -> None:
    """Build every subsystem and attach to the running ComfyUI server."""
    global _config, _sampler, _store, _api, _hooks, _started  # noqa: PLW0603
    with _lock:
        if _started:
            return
        _started = True
        try:
            _config = Config(CONFIG_PATH)
            _sampler = MetricsSampler(_config)
            _store = RunStore(_config, _sampler, LOG_DIR)
            _api = MonitorAPI(_config, _sampler, _store)
            _hooks = ComfyHooks(_store, _sampler, _config)

            if _config.get("enabled", True):
                _sampler.start()
            # Routes first, then hooks: a failure in one still leaves the other.
            try:
                _api.register()
            except Exception as exc:
                logger.warning("[sysmon] route registration failed: %s", exc)
            try:
                _hooks.install()
            except Exception as exc:
                logger.warning("[sysmon] hook installation failed: %s", exc)
            logger.info("[sysmon] v%s ready (gpu backend: %s)", __version__, _sampler.stats().get("gpu_backend"))
        except Exception as exc:
            logger.warning("[sysmon] failed to start: %s", exc)


def _boot_when_ready() -> None:
    """Wait for PromptServer, then boot.

    Custom nodes are imported before ComfyUI finishes building its server, so
    importing ``server`` here (rather than at module import time) is what keeps
    us attached to the real instance.
    """
    deadline = time.time() + 120.0
    while time.time() < deadline:
        try:
            from server import PromptServer  # type: ignore

            if getattr(PromptServer, "instance", None) is not None:
                _boot()
                return
        except Exception:
            pass
        time.sleep(0.25)
    # Timed out: still boot so at least metrics and persistence work.
    _boot()


threading.Thread(target=_boot_when_ready, name="sysmon-boot", daemon=True).start()


# --------------------------------------------------------------------------
# Optional node: surface live metrics inside a workflow
# --------------------------------------------------------------------------
class SystemMonitorReport:
    """Reads the monitor's current numbers and passes them along a graph.

    Useful for writing metrics into a filename, or for branching a workflow on
    VRAM pressure.
    """

    CATEGORY = "utils/monitor"
    FUNCTION = "report"
    RETURN_TYPES = ("FLOAT", "FLOAT", "FLOAT", "FLOAT", "STRING")
    RETURN_NAMES = ("gpu_percent", "vram_mb", "cpu_percent", "ram_percent", "text")
    OUTPUT_NODE = True

    @classmethod
    def INPUT_TYPES(cls) -> Dict[str, Any]:
        return {
            "required": {
                "scope": (["live", "last_run"], {"default": "live"}),
            },
            "optional": {
                "run_index": ("INT", {"default": 0, "min": 0, "max": 999,
                                      "tooltip": "0 = most recent finished run"}),
            },
        }

    @classmethod
    def IS_CHANGED(cls, **kwargs):  # noqa: N802
        # Always re-run: this node reports a live reading.
        return float("nan")

    def report(self, scope: str = "live", run_index: int = 0) -> Tuple[float, float, float, float, str]:
        try:
            if _store is None or _sampler is None:
                return (0.0, 0.0, 0.0, 0.0, "System Monitor is not running")
            if scope == "last_run":
                runs = _store.history(max(1, run_index + 1))
                if not runs:
                    return (0.0, 0.0, 0.0, 0.0, "No finished runs recorded yet")
                run = runs[min(run_index, len(runs) - 1)]
                peaks = run.get("peaks") or {}
                gpu = (peaks.get("gpu_util") or {}).get("value") or 0.0
                vram = (peaks.get("vram_used") or {}).get("value") or 0.0
                cpu = (peaks.get("cpu") or {}).get("value") or 0.0
                ram = (peaks.get("ram_used") or {}).get("percent") or 0.0
                text = _format_run(run)
                return (float(gpu), float(vram), float(cpu), float(ram), text)

            sample = _sampler.latest() or {}
            gpu_info = sample.get("gpu") or {}
            gpu = float(gpu_info.get("util_percent") or 0.0)
            vram = float(gpu_info.get("mem_used_mb") or 0.0)
            cpu = float(sample.get("cpu_percent") or 0.0)
            ram = float(sample.get("ram_percent") or 0.0)
            text = _format_live(sample)
            return (gpu, vram, cpu, ram, text)
        except Exception as exc:  # a monitoring node must never fail a graph
            logger.warning("[sysmon] node report failed: %s", exc)
            return (0.0, 0.0, 0.0, 0.0, f"System Monitor error: {exc}")


def _format_live(sample: Dict[str, Any]) -> str:
    if not sample:
        return "No sample yet"
    gpu = sample.get("gpu") or {}
    lines = []
    if gpu:
        total = gpu.get("mem_total_mb") or 0
        used = gpu.get("mem_used_mb") or 0
        pct = (used / total * 100.0) if total else 0.0
        lines.append(f"GPU {gpu.get('util_percent')}%  VRAM {used:.0f}/{total:.0f} MB ({pct:.0f}%)")
        if gpu.get("temperature_c") is not None:
            lines.append(f"Temp {gpu['temperature_c']:.0f}C  Power {gpu.get('power_w') or 0:.0f}W")
    else:
        # Say so explicitly: silently omitting the GPU leaves a user on a machine
        # without an NVIDIA driver unsure whether monitoring is broken.
        lines.append("GPU: not available (no NVIDIA driver detected)")
    lines.append(f"CPU {sample.get('cpu_percent')}%  RAM {sample.get('ram_percent')}%")
    read = sample.get("disk_read_mb_s")
    write = sample.get("disk_write_mb_s")
    if read is not None and write is not None:
        lines.append(f"Disk R {read:.1f} MB/s  W {write:.1f} MB/s")
    return "\n".join(lines)


def _format_run(run: Dict[str, Any]) -> str:
    peaks = run.get("peaks") or {}
    headline = f"run #{run.get('index')}"
    workflow = run.get("workflow")
    if workflow:
        headline += f" [{workflow}]"
    parts = [f"{headline} {run.get('status')} in {run.get('duration_s')}s"]
    for key, label in (
        ("gpu_util", "GPU"),
        ("vram_used", "VRAM"),
        ("cpu", "CPU"),
        ("ram_used", "RAM"),
    ):
        entry = peaks.get(key)
        if entry:
            parts.append(f"{label} peak {entry.get('value')}{entry.get('unit')}")
    return "  ".join(parts)


NODE_CLASS_MAPPINGS = {
    "SystemMonitorReport": SystemMonitorReport,
}

NODE_DISPLAY_NAME_MAPPINGS = {
    "SystemMonitorReport": "System Monitor (Report)",
}

__all__ = ["NODE_CLASS_MAPPINGS", "NODE_DISPLAY_NAME_MAPPINGS", "WEB_DIRECTORY", "__version__"]
