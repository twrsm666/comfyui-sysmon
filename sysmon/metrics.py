"""Hardware sampling: GPU, VRAM, CPU, RAM and disk throughput.

Deliberately free of ComfyUI imports so it can be exercised headlessly.

Three GPU backends are attempted in order of preference:

1. ``pynvml`` / ``nvidia-ml-py``  - richest data, no subprocess per sample
2. ``nvidia-smi`` (``--query-gpu`` CSV) - always present alongside a driver
3. ``torch.cuda``               - VRAM only, no utilisation/temperature

If no backend works the sampler still reports CPU/RAM/disk and marks the GPU
section unavailable, so the panel degrades instead of breaking.
"""

from __future__ import annotations

import glob
import os
import shutil
import subprocess
import sys
import threading
import time
from collections import deque
from typing import Any, Deque, Dict, List, Optional, Tuple

try:  # psutil is a hard requirement of modern ComfyUI, but stay defensive
    import psutil
except Exception:  # pragma: no cover
    psutil = None  # type: ignore

_BYTES_PER_MB = 1024.0 * 1024.0


# --------------------------------------------------------------------------
# GPU backend: NVML
# --------------------------------------------------------------------------
class NvmlBackend:
    """Query the NVIDIA driver directly through NVML."""

    name = "pynvml"

    def __init__(self) -> None:
        self._nvml = None
        self._count = 0
        for module_name in ("pynvml", "nvidia_ml_py", "py3nvml.py3nvml"):
            try:
                module = __import__(module_name, fromlist=["nvmlInit"])
            except Exception:
                continue
            try:
                module.nvmlInit()
                self._count = module.nvmlDeviceGetCount()
            except Exception:
                continue
            self._nvml = module
            break

    @property
    def available(self) -> bool:
        return self._nvml is not None and self._count > 0

    def read(self, index: int) -> Optional[Dict[str, Any]]:
        nvml = self._nvml
        if nvml is None or index >= self._count:
            return None
        try:
            handle = nvml.nvmlDeviceGetHandleByIndex(index)
            mem = nvml.nvmlDeviceGetMemoryInfo(handle)
            info: Dict[str, Any] = {
                "index": index,
                "name": _decode(nvml.nvmlDeviceGetName(handle)),
                "mem_total_mb": mem.total / _BYTES_PER_MB,
                "mem_used_mb": mem.used / _BYTES_PER_MB,
                "mem_free_mb": mem.free / _BYTES_PER_MB,
            }
            info["util_percent"] = _safe(
                lambda: float(nvml.nvmlDeviceGetUtilizationRates(handle).gpu)
            )
            info["mem_util_percent"] = _safe(
                lambda: float(nvml.nvmlDeviceGetUtilizationRates(handle).memory)
            )
            info["temperature_c"] = _safe(
                lambda: float(nvml.nvmlDeviceGetTemperature(handle, nvml.NVML_TEMPERATURE_GPU))
            )
            info["power_w"] = _safe(
                lambda: float(nvml.nvmlDeviceGetPowerUsage(handle)) / 1000.0
            )
            info["power_limit_w"] = _safe(
                lambda: float(nvml.nvmlDeviceGetEnforcedPowerLimit(handle)) / 1000.0
            )
            info["fan_percent"] = _safe(
                lambda: float(nvml.nvmlDeviceGetFanSpeed(handle))
            )
            info["clock_sm_mhz"] = _safe(
                lambda: float(
                    nvml.nvmlDeviceGetClockInfo(handle, nvml.NVML_CLOCK_SM)
                )
            )
            # Per-process VRAM lets us separate ComfyUI from other consumers.
            try:
                procs = nvml.nvmlDeviceGetComputeRunningProcesses(handle)
                info["processes"] = [
                    {
                        "pid": int(p.pid),
                        "used_mb": (p.usedGpuMemory or 0) / _BYTES_PER_MB,
                    }
                    for p in procs
                    if p.usedGpuMemory
                ]
            except Exception:
                info["processes"] = []
            return info
        except Exception:
            return None


# --------------------------------------------------------------------------
# GPU backend: nvidia-smi
# --------------------------------------------------------------------------
_SMI_FIELDS = [
    "index",
    "name",
    "utilization.gpu",
    "utilization.memory",
    "memory.total",
    "memory.used",
    "memory.free",
    "temperature.gpu",
    "power.draw",
    "power.limit",
    "fan.speed",
    "clocks.sm",
]


class SmiBackend:
    """Fall back to the ``nvidia-smi`` CLI shipped with every NVIDIA driver."""

    name = "nvidia-smi"

    def __init__(self) -> None:
        self.exe = _find_nvidia_smi()
        self._cache: List[Dict[str, Any]] = []
        self._cache_ts = 0.0
        self._lock = threading.Lock()

    @property
    def available(self) -> bool:
        return bool(self.exe)

    def refresh(self) -> None:
        """Call nvidia-smi once and cache every GPU's readings."""
        if not self.exe:
            return
        query = ",".join(_SMI_FIELDS)
        cmd = [
            self.exe,
            f"--query-gpu={query}",
            "--format=csv,noheader,nounits",
        ]
        creationflags = 0x08000000 if os.name == "nt" else 0  # CREATE_NO_WINDOW
        try:
            out = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=10,
                creationflags=creationflags,
            )
            if out.returncode != 0:
                return
            parsed = _parse_smi_csv(out.stdout)
            with self._lock:
                self._cache = parsed
                self._cache_ts = time.monotonic()
        except Exception:
            return

    def read(self, index: int) -> Optional[Dict[str, Any]]:
        with self._lock:
            cache = list(self._cache)
        for entry in cache:
            if entry.get("index") == index:
                return entry
        # Index not seen yet - force one refresh and retry.
        if not cache:
            self.refresh()
            with self._lock:
                for entry in self._cache:
                    if entry.get("index") == index:
                        return entry
        return None

    def all(self) -> List[Dict[str, Any]]:
        with self._lock:
            return list(self._cache)


def _parse_smi_csv(text: str) -> List[Dict[str, Any]]:
    gpus: List[Dict[str, Any]] = []
    for line in (text or "").splitlines():
        line = line.strip()
        if not line:
            continue
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < len(_SMI_FIELDS):
            continue
        raw = dict(zip(_SMI_FIELDS, parts))
        gpus.append(
            {
                "index": int(_num(raw["index"]) or 0),
                "name": raw["name"],
                "util_percent": _num(raw["utilization.gpu"]),
                "mem_util_percent": _num(raw["utilization.memory"]),
                "mem_total_mb": _num(raw["memory.total"]),
                "mem_used_mb": _num(raw["memory.used"]),
                "mem_free_mb": _num(raw["memory.free"]),
                "temperature_c": _num(raw["temperature.gpu"]),
                "power_w": _num(raw["power.draw"]),
                "power_limit_w": _num(raw["power.limit"]),
                "fan_percent": _num(raw["fan.speed"]),
                "clock_sm_mhz": _num(raw["clocks.sm"]),
                "processes": [],
            }
        )
    return gpus


def _find_nvidia_smi() -> Optional[str]:
    found = shutil.which("nvidia-smi")
    if found:
        return found
    candidates = [
        r"C:\Windows\System32\nvidia-smi.exe",
        r"C:\Program Files\NVIDIA Corporation\NVSMI\nvidia-smi.exe",
        "/usr/bin/nvidia-smi",
        "/usr/local/bin/nvidia-smi",
        "/usr/local/cuda/bin/nvidia-smi",
    ]
    for path in candidates:
        if os.path.isfile(path):
            return path
    # Last resort: inside the WSL/driver library dirs on some Linux images.
    for pattern in ("/usr/lib/wsl/lib/nvidia-smi", "/opt/nvidia/*/nvidia-smi"):
        for path in glob.glob(pattern):
            if os.path.isfile(path):
                return path
    return None


# --------------------------------------------------------------------------
# GPU backend: torch
# --------------------------------------------------------------------------
class TorchBackend:
    """VRAM-only fallback via torch, used when NVML and nvidia-smi both fail."""

    name = "torch"

    def __init__(self) -> None:
        self._torch = None
        try:
            import torch  # noqa: WPS433 (optional import by design)

            if torch.cuda.is_available():
                self._torch = torch
        except Exception:
            self._torch = None

    @property
    def available(self) -> bool:
        return self._torch is not None

    def read(self, index: int) -> Optional[Dict[str, Any]]:
        if self._torch is None:
            return None
        try:
            torch = self._torch
            free, total = torch.cuda.mem_get_info(index)
            return {
                "index": index,
                "name": torch.cuda.get_device_name(index),
                "util_percent": None,
                "mem_util_percent": None,
                "mem_total_mb": total / _BYTES_PER_MB,
                "mem_used_mb": (total - free) / _BYTES_PER_MB,
                "mem_free_mb": free / _BYTES_PER_MB,
                "temperature_c": None,
                "power_w": None,
                "power_limit_w": None,
                "fan_percent": None,
                "clock_sm_mhz": None,
                "processes": [],
            }
        except Exception:
            return None


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------
def _decode(value: Any) -> str:
    if isinstance(value, bytes):
        try:
            return value.decode("utf-8", "replace")
        except Exception:
            return str(value)
    return str(value)


def _num(text: Any) -> Optional[float]:
    """Parse an nvidia-smi cell; ``[N/A]`` and friends become None."""
    if text is None:
        return None
    cleaned = str(text).strip()
    if not cleaned or cleaned.startswith("[") or cleaned.lower() in ("n/a", "na"):
        return None
    try:
        return float(cleaned)
    except ValueError:
        return None


def _safe(fn) -> Optional[float]:
    try:
        value = fn()
        return None if value is None else float(value)
    except Exception:
        return None


# --------------------------------------------------------------------------
# The sampler
# --------------------------------------------------------------------------
class MetricsSampler:
    """Background thread producing timestamped hardware samples.

    Samples are stored in a ring buffer keyed by a monotonically increasing
    index so that the run recorder can slice out exactly the window a single
    node occupied without worrying about eviction.
    """

    def __init__(self, config) -> None:
        self.config = config
        self.interval_ms = int(config.get("sample_interval_ms") or 1000)
        self.history_size = int(config.get("history_size") or 900)
        self._buf: Deque[Dict[str, Any]] = deque(maxlen=self.history_size)
        self._base = 0            # ring index of _buf[0]
        self._next = 0            # ring index that the next sample will get
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._idle = threading.Event()
        self._nvml = NvmlBackend()
        self._smi = SmiBackend()
        self._torch = TorchBackend()
        self._gpu_backend: Optional[Any] = None
        self._gpu_static: Dict[str, Any] = {}
        self._gpu_series_added = False
        self._last_disk: Optional[Tuple[float, int, int]] = None
        self._last_net: Optional[Tuple[float, int, int]] = None
        self._last_cpu_ts: Optional[float] = None
        self._proc = psutil.Process(os.getpid()) if psutil else None
        if self._proc is not None:
            try:
                self._proc.cpu_percent(None)  # prime the delta
            except Exception:
                self._proc = None

    # -- lifecycle --------------------------------------------------------
    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._select_gpu_backend()
        # Prime cumulative counters so rate metrics have a baseline.
        self._read_disk()
        # Take one synchronous sample so ``latest()`` is populated immediately.
        # Without this the panel (and the report node) shows "no data" for up to
        # a full sample interval after startup.
        self._record(self.sample_once())
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop, name="sysmon-sampler", daemon=True
        )
        self._thread.start()

    def _record(self, sample: Dict[str, Any]) -> Dict[str, Any]:
        """Append a sample to the ring buffer, assigning its index."""
        with self._lock:
            sample["i"] = self._next
            self._buf.append(sample)
            self._next += 1
            self._base = self._next - len(self._buf)
            return sample

    def stop(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread and thread.is_alive():
            thread.join(timeout=5.0)
        self._thread = None

    @property
    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def set_active(self, active: bool) -> None:
        """Switch between idle and in-execution sampling cadence."""
        if active:
            self._idle.clear()
        else:
            self._idle.set()

    def apply_config(self) -> None:
        """Re-read tunables after a settings change."""
        self.interval_ms = max(200, int(self.config.get("sample_interval_ms") or 1000))
        new_size = max(60, int(self.config.get("history_size") or 900))
        with self._lock:
            if new_size != self.history_size:
                self.history_size = new_size
                keep = list(self._buf)[-new_size:]
                self._base = self._next - len(keep)
                self._buf = deque(keep, maxlen=new_size)
        if not self.config.get("enabled", True):
            self.stop()
        else:
            self.start()

    # -- backend selection ------------------------------------------------
    def _select_gpu_backend(self) -> None:
        if self._gpu_backend is not None:
            return
        for backend in (self._nvml, self._smi, self._torch):
            if backend.available:
                self._gpu_backend = backend
                break

    def _gpu_index(self) -> int:
        configured = self.config.get("gpu_index")
        if configured is None or configured == "":
            return 0
        try:
            return int(configured)
        except (TypeError, ValueError):
            return 0

    # -- sampling loop ----------------------------------------------------
    def _loop(self) -> None:
        while not self._stop.is_set():
            started = time.perf_counter()
            try:
                self._record(self.sample_once())
            except Exception:
                pass
            interval = self.interval_ms if self._idle.is_set() else int(
                self.config.get("sample_interval_active_ms") or self.interval_ms
            )
            elapsed_ms = (time.perf_counter() - started) * 1000.0
            self._stop.wait(max(0.05, (interval - elapsed_ms) / 1000.0))

    def sample_once(self) -> Dict[str, Any]:
        """Collect a single sample. Exposed for tests and on-demand reads."""
        now = time.perf_counter()
        sample: Dict[str, Any] = {
            "t": round(now, 4),
            "wall": time.time(),
        }
        sample.update(self._read_cpu())
        sample.update(self._read_ram())
        sample.update(self._read_disk())
        sample["gpu"] = self._read_gpu()
        if self._proc is not None:
            ram = sample.get("ram_used_mb") or 0.0
            sample["proc"] = {
                "cpu_percent": _safe(lambda: self._proc.cpu_percent(None)),
                "rss_mb": _safe(lambda: self._proc.memory_info().rss / _BYTES_PER_MB),
                "num_threads": _safe(lambda: float(self._proc.num_threads())),
            }
            _ = ram
        return sample

    # -- individual readers ----------------------------------------------
    def _read_cpu(self) -> Dict[str, Any]:
        if psutil is None:
            return {"cpu_percent": None, "cpu_per_core": [], "cpu_count": 0}
        try:
            pct = psutil.cpu_percent(None)
            per_core = psutil.cpu_percent(None, percpu=True)
            freq = psutil.cpu_freq(None)
            return {
                "cpu_percent": float(pct),
                "cpu_per_core": [float(x) for x in per_core],
                "cpu_count": psutil.cpu_count(logical=True) or 0,
                "cpu_phys": psutil.cpu_count(logical=False) or 0,
                "cpu_freq_mhz": float(freq.current) if freq else None,
            }
        except Exception:
            return {"cpu_percent": None, "cpu_per_core": [], "cpu_count": 0}

    def _read_ram(self) -> Dict[str, Any]:
        if psutil is None:
            return {"ram_used_mb": None, "ram_total_mb": None, "ram_percent": None}
        try:
            vm = psutil.virtual_memory()
            sw = None
            try:
                sw = psutil.swap_memory()
            except Exception:
                pass
            return {
                "ram_used_mb": (vm.total - vm.available) / _BYTES_PER_MB,
                "ram_total_mb": vm.total / _BYTES_PER_MB,
                "ram_available_mb": vm.available / _BYTES_PER_MB,
                "ram_percent": float(vm.percent),
                "swap_percent": float(sw.percent) if sw else None,
            }
        except Exception:
            return {"ram_used_mb": None, "ram_total_mb": None, "ram_percent": None}

    def _read_disk(self) -> Dict[str, Any]:
        """Report throughput in MB/s as a delta of cumulative counters."""
        if psutil is None:
            return {"disk_read_mb_s": None, "disk_write_mb_s": None}
        now = time.perf_counter()
        try:
            target = (self.config.get("disk_path") or "").strip()
            if target:
                counters = self._counter_for_path(target)
            else:
                counters = psutil.disk_io_counters(perdisk=False)
            if counters is None:
                return {"disk_read_mb_s": None, "disk_write_mb_s": None}
            read_bytes = int(getattr(counters, "read_bytes", 0))
            write_bytes = int(getattr(counters, "write_bytes", 0))
            read_mb_s = write_mb_s = None
            if self._last_disk is not None:
                prev_t, prev_r, prev_w = self._last_disk
                dt = now - prev_t
                if dt > 1e-6:
                    read_mb_s = max(0.0, (read_bytes - prev_r) / _BYTES_PER_MB / dt)
                    write_mb_s = max(0.0, (write_bytes - prev_w) / _BYTES_PER_MB / dt)
            self._last_disk = (now, read_bytes, write_bytes)
            total = None
            if read_mb_s is not None and write_mb_s is not None:
                total = read_mb_s + write_mb_s
            return {
                "disk_read_mb_s": read_mb_s,
                "disk_write_mb_s": write_mb_s,
                "disk_total_mb_s": total,
                "disk_path": target or "all",
            }
        except Exception:
            return {"disk_read_mb_s": None, "disk_write_mb_s": None}

    def _counter_for_path(self, target: str):
        """Best-effort mapping from a filesystem path to a physical disk."""
        if psutil is None:
            return None
        try:
            target = os.path.abspath(target)
            best = None
            best_len = -1
            for part in psutil.disk_partitions(all=False):
                mount = os.path.abspath(part.mountpoint)
                if target.lower().startswith(mount.lower()) and len(mount) > best_len:
                    best, best_len = part, len(mount)
            if best is None:
                return psutil.disk_io_counters(perdisk=False)
            per_disk = psutil.disk_io_counters(perdisk=True) or {}
            device = (best.device or "").replace("\\\\.\\", "")
            if os.name == "nt":
                # Windows devices look like "PhysicalDrive0"; psutil keys on that.
                key = device
                if key in per_disk:
                    return per_disk[key]
                # Partition mountpoints are drive letters; fall back to totals.
                return psutil.disk_io_counters(perdisk=False)
            key = os.path.basename(device)
            if key in per_disk:
                return per_disk[key]
            return psutil.disk_io_counters(perdisk=False)
        except Exception:
            return psutil.disk_io_counters(perdisk=False)

    def _read_gpu(self) -> Optional[Dict[str, Any]]:
        self._select_gpu_backend()
        backend = self._gpu_backend
        if backend is None:
            return None
        if backend is self._smi:
            backend.refresh()
        index = self._gpu_index()
        info = backend.read(index)
        if info is None:
            return None
        if not self._gpu_series_added:
            self._gpu_static = {
                "name": info.get("name"),
                "mem_total_mb": info.get("mem_total_mb"),
                "backend": backend.name,
            }
            self._gpu_series_added = True
        # Attach this process's own VRAM share when NVML gave us per-process data.
        info = dict(info)
        if info.get("processes"):
            mine = [p for p in info["processes"] if p.get("pid") == os.getpid()]
            info["self_used_mb"] = mine[0]["used_mb"] if mine else 0.0
        else:
            info["self_used_mb"] = None
        return info

    # -- reads ------------------------------------------------------------
    def latest(self) -> Optional[Dict[str, Any]]:
        with self._lock:
            return dict(self._buf[-1]) if self._buf else None

    def window(self, t_from: float, t_to: float) -> List[Dict[str, Any]]:
        """All samples whose ``t`` (perf_counter) falls inside the window."""
        with self._lock:
            return [s for s in self._buf if t_from <= s.get("t", 0.0) <= t_to]

    def series(self, limit: int = 240) -> List[Dict[str, Any]]:
        with self._lock:
            return [dict(s) for s in list(self._buf)[-limit:]]

    def stats(self) -> Dict[str, Any]:
        with self._lock:
            count = len(self._buf)
            base = self._base
        return {
            "running": self.running,
            "samples": count,
            "ring_base": base,
            "gpu_backend": self._gpu_backend.name if self._gpu_backend else None,
            "gpu": dict(self._gpu_static),
            "has_psutil": psutil is not None,
            "interval_ms": self.interval_ms,
            "history_size": self.history_size,
            "python": sys.version.split()[0],
        }


# --------------------------------------------------------------------------
# Peak extraction
# --------------------------------------------------------------------------
# Each metric: (key, human label, series path, unit, higher_is_worse)
METRIC_DEFS: List[Tuple[str, str, Tuple[str, ...], str, bool]] = [
    ("gpu_util", "GPU 利用率", ("gpu", "util_percent"), "%", True),
    ("vram_used", "显存占用", ("gpu", "mem_used_mb"), "MB", True),
    ("gpu_temp", "GPU 温度", ("gpu", "temperature_c"), "°C", True),
    ("gpu_power", "GPU 功耗", ("gpu", "power_w"), "W", True),
    ("cpu", "CPU 占用", ("cpu_percent",), "%", True),
    ("ram_used", "内存占用", ("ram_used_mb",), "MB", True),
    ("disk_read", "磁盘读取", ("disk_read_mb_s",), "MB/s", True),
    ("disk_write", "磁盘写入", ("disk_write_mb_s",), "MB/s", True),
]


def _dig(sample: Dict[str, Any], path: Tuple[str, ...]) -> Optional[float]:
    node: Any = sample
    for key in path:
        if not isinstance(node, dict):
            return None
        node = node.get(key)
    if node is None:
        return None
    try:
        return float(node)
    except (TypeError, ValueError):
        return None


def extract_peaks(samples: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Max of every metric across ``samples`` plus the time it happened."""
    peaks: Dict[str, Any] = {}
    for key, label, path, unit, _worse in METRIC_DEFS:
        best_value: Optional[float] = None
        best_sample: Optional[Dict[str, Any]] = None
        for sample in samples:
            value = _dig(sample, path)
            if value is None:
                continue
            if best_value is None or value > best_value:
                best_value, best_sample = value, sample
        if best_value is None:
            continue
        peaks[key] = {
            "label": label,
            "value": round(best_value, 2),
            "unit": unit,
            "t": best_sample.get("t") if best_sample else None,
            "wall": best_sample.get("wall") if best_sample else None,
            "percent": _peak_percent(key, best_value, best_sample),
        }
    return peaks


def _peak_percent(key: str, value: float, sample: Optional[Dict[str, Any]]) -> Optional[float]:
    """Express a peak as a percentage of capacity where that is meaningful."""
    if sample is None:
        return None
    if key == "vram_used":
        total = _dig(sample, ("gpu", "mem_total_mb"))
        return round(value / total * 100.0, 1) if total else None
    if key == "ram_used":
        total = _dig(sample, ("ram_total_mb",))
        return round(value / total * 100.0, 1) if total else None
    if key in ("gpu_util", "cpu"):
        return round(value, 1)
    return None


def summarize(samples: List[Dict[str, Any]], metric: str) -> Dict[str, Any]:
    """Average / max / min for one metric key over a window."""
    path = next((p for k, _l, p, _u, _w in METRIC_DEFS if k == metric), None)
    if path is None:
        return {}
    values = [v for v in (_dig(s, path) for s in samples) if v is not None]
    if not values:
        return {}
    return {
        "avg": round(sum(values) / len(values), 2),
        "max": round(max(values), 2),
        "min": round(min(values), 2),
        "n": len(values),
    }
