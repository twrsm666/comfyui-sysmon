"""Run history: per-node peak attribution, durations and error records.

A *run* is one prompt execution.  The recorder is fed by two sources:

* hardware samples from :mod:`sysmon.metrics` (a background thread), and
* lifecycle events from ComfyUI's ``PromptExecutor`` (see :mod:`sysmon.hooks`).

Because sampling is asynchronous, a node's peak is the maximum observed inside
the wall-clock window the node occupied.  A 1 Hz sampler can therefore miss
spikes inside very short nodes; the sampler automatically switches to a faster
cadence while a prompt is executing to keep that window small.  This is
recorded per run as ``sample_interval_ms`` so the UI can be honest about it.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional

from .metrics import METRIC_DEFS, extract_peaks, summarize

MAX_TRACEBACK_LINES = 40
MAX_NODES_PER_RUN = 400
_UNSAFE = re.compile(r"[^A-Za-z0-9._-]+")


def _safe_name(text: str, limit: int = 60) -> str:
    cleaned = _UNSAFE.sub("_", (text or "").strip())[:limit].strip("._-")
    return cleaned or "workflow"


# --------------------------------------------------------------------------
# Records
# --------------------------------------------------------------------------
@dataclass
class NodeStat:
    """What one node cost, and the peaks observed while it ran."""

    node_id: str
    class_type: str = ""
    title: str = ""
    duration_s: Optional[float] = None
    t_start: Optional[float] = None
    t_end: Optional[float] = None
    samples: int = 0
    cached: bool = False
    peaks: Dict[str, Any] = field(default_factory=dict)
    summary: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class RunError:
    node_id: Optional[str] = None
    node_type: str = ""
    node_title: str = ""
    exception_type: str = ""
    exception_message: str = ""
    traceback: List[str] = field(default_factory=list)
    kind: str = "error"  # error | interrupted
    t: float = 0.0
    current_inputs: Any = None
    executed: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class RunRecord:
    prompt_id: str
    index: int
    started_wall: float
    status: str = "running"  # running | success | error | interrupted
    ended_wall: Optional[float] = None
    duration_s: Optional[float] = None
    workflow: str = ""
    node_count: int = 0
    executed_count: int = 0
    cached_count: int = 0
    sample_interval_ms: int = 1000
    gpu_backend: Optional[str] = None
    gpu_name: Optional[str] = None
    nodes: List[NodeStat] = field(default_factory=list)
    peaks: Dict[str, Any] = field(default_factory=dict)
    errors: List[RunError] = field(default_factory=list)
    timeline: List[Dict[str, Any]] = field(default_factory=list)
    saved_path: Optional[str] = None
    analysis: Optional[Dict[str, Any]] = None

    def to_dict(self, include_timeline: bool = False) -> Dict[str, Any]:
        data = {
            "prompt_id": self.prompt_id,
            "index": self.index,
            "started_wall": self.started_wall,
            "started_iso": _iso(self.started_wall),
            "ended_wall": self.ended_wall,
            "ended_iso": _iso(self.ended_wall) if self.ended_wall else None,
            "status": self.status,
            "duration_s": self.duration_s,
            "workflow": self.workflow,
            "node_count": self.node_count,
            "executed_count": self.executed_count,
            "cached_count": self.cached_count,
            "sample_interval_ms": self.sample_interval_ms,
            "gpu_backend": self.gpu_backend,
            "gpu_name": self.gpu_name,
            "nodes": [n.to_dict() for n in self.nodes],
            "peaks": self.peaks,
            "errors": [e.to_dict() for e in self.errors],
            "saved_path": self.saved_path,
            "analysis": self.analysis,
        }
        if include_timeline:
            data["timeline"] = self.timeline
        return data


def _iso(ts: Optional[float]) -> Optional[str]:
    if not ts:
        return None
    try:
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts))
    except Exception:
        return None


# --------------------------------------------------------------------------
# Recorder
# --------------------------------------------------------------------------
class RunStore:
    """Thread-safe store of the current run plus a bounded history."""

    def __init__(self, config, sampler, log_dir: str):
        self.config = config
        self.sampler = sampler
        self.log_dir = log_dir
        os.makedirs(self.log_dir, exist_ok=True)
        self._lock = threading.RLock()
        self._history: List[RunRecord] = []
        self._current: Optional[RunRecord] = None
        self._open_nodes: Dict[str, Dict[str, Any]] = {}
        self._titles: Dict[str, str] = {}
        self._types: Dict[str, str] = {}
        self._cached_nodes: set = set()
        self._counter = 0
        self._pending_analysis: Dict[str, Any] = {}

    # -- lifecycle --------------------------------------------------------
    def begin_run(
        self,
        prompt_id: str,
        prompt: Optional[Dict[str, Any]] = None,
        extra_data: Optional[Dict[str, Any]] = None,
    ) -> RunRecord:
        with self._lock:
            self._counter += 1
            workflow = _workflow_name(extra_data)
            self._titles, self._types = _titles_from(extra_data, prompt)
            self._cached_nodes = set()
            self._open_nodes = {}
            record = RunRecord(
                prompt_id=prompt_id,
                index=self._counter,
                started_wall=time.time(),
                workflow=workflow,
                node_count=len(prompt or {}),
                sample_interval_ms=int(
                    self.config.get("sample_interval_active_ms")
                    or self.config.get("sample_interval_ms")
                    or 1000
                ),
            )
            stats = self.sampler.stats()
            record.gpu_backend = stats.get("gpu_backend")
            record.gpu_name = (stats.get("gpu") or {}).get("name")
            self._current = record
            self.sampler.set_active(True)
            return record

    def note_cached(self, node_ids: List[str]) -> None:
        with self._lock:
            self._cached_nodes.update(str(n) for n in (node_ids or []))

    def note_node_start(self, node_id: str, class_type: str = "", title: str = "") -> None:
        with self._lock:
            if self._current is None:
                return
            key = str(node_id)
            if key in self._cached_nodes:
                # Cached nodes emit "executed" with no "executing"; skip them.
                return
            if key in self._open_nodes:
                # Re-entered without a matching end (lazy/partial execution).
                self.note_node_end(key)
            self._open_nodes[key] = {
                "t": time.perf_counter(),
                # The prompt dict is the authoritative source for class_type;
                # the frontend workflow JSON may not carry it at all.
                "class_type": class_type or self._types.get(key, ""),
                "title": title or self._titles.get(key) or class_type or f"#{key}",
            }

    def note_node_end(self, node_id: str) -> None:
        with self._lock:
            record = self._current
            key = str(node_id)
            opened = self._open_nodes.pop(key, None)
            if record is None or opened is None:
                return
            if len(record.nodes) >= MAX_NODES_PER_RUN:
                return
            t_end = time.perf_counter()
            t_start = opened["t"]
            samples = self.sampler.window(t_start, t_end)
            node = NodeStat(
                node_id=key,
                class_type=opened.get("class_type") or "",
                title=opened.get("title") or f"#{key}",
                t_start=round(t_start, 4),
                t_end=round(t_end, 4),
                duration_s=round(max(0.0, t_end - t_start), 3),
                samples=len(samples),
                peaks=extract_peaks(samples),
                summary={
                    key: summarize(samples, key)
                    for key in ("gpu_util", "vram_used", "cpu", "ram_used")
                },
            )
            record.nodes.append(node)

    def note_error(self, data: Dict[str, Any], kind: str = "error") -> Optional[RunError]:
        """Record a failure. ``data`` is ComfyUI's execution_error payload."""
        with self._lock:
            record = self._current
            node_id = data.get("node_id")
            node_id = None if node_id is None else str(node_id)
            error = RunError(
                node_id=node_id,
                node_type=str(data.get("node_type") or ""),
                node_title=self._titles.get(node_id or "") or str(data.get("node_type") or ""),
                exception_type=str(data.get("exception_type") or kind),
                exception_message=str(
                    data.get("exception_message") or data.get("message") or ""
                )[:8000],
                traceback=_traceback_lines(data.get("traceback")),
                kind=kind,
                t=time.time(),
                current_inputs=_shrink(data.get("current_inputs")),
                executed=[str(x) for x in (data.get("executed") or [])][:MAX_NODES_PER_RUN],
            )
            if record is not None:
                record.errors.append(error)
            return error

    def end_run(self, status: str, prompt_id: str = "") -> Optional[RunRecord]:
        with self._lock:
            record = self._current
            if record is None:
                return None
            if prompt_id and record.prompt_id != prompt_id:
                return None
            # Close any node that never reported an end (interrupt/exception).
            for key in list(self._open_nodes.keys()):
                self.note_node_end(key)
            record.ended_wall = time.time()
            record.duration_s = round(record.ended_wall - record.started_wall, 3)
            record.status = status
            record.executed_count = len(record.nodes)
            record.cached_count = len(self._cached_nodes)
            # Run-level peaks come from the full window, then we keep only the
            # heavy hitters so the payload stays small.
            window = _window_from_nodes(self.sampler, record)
            record.peaks = extract_peaks(window)
            self._history.append(record)
            self._history = self._history[-_history_cap(self.config):]
            self._current = None
            self._open_nodes = {}
            self.sampler.set_active(False)
            self._persist(record)
            return record

    def after_run_cleanup(self, status: str, prompt_id: str = "") -> None:
        """Called from the executor's ``finally`` so nothing is left dangling."""
        with self._lock:
            if self._current is not None and (
                not prompt_id or self._current.prompt_id == prompt_id
            ):
                if self._current.status == "running":
                    self.end_run(status, prompt_id)

    # -- queries ----------------------------------------------------------
    def current(self) -> Optional[RunRecord]:
        with self._lock:
            return self._current

    def history(self, limit: int = 10) -> List[Dict[str, Any]]:
        with self._lock:
            runs = list(self._history)[-limit:]
        return [r.to_dict() for r in reversed(runs)]

    def get(self, prompt_id: str) -> Optional[RunRecord]:
        with self._lock:
            if self._current is not None and self._current.prompt_id == prompt_id:
                return self._current
            for record in self._history:
                if record.prompt_id == prompt_id:
                    return record
        return None

    def latest_finished(self) -> Optional[RunRecord]:
        with self._lock:
            return self._history[-1] if self._history else None

    def live(self) -> Dict[str, Any]:
        """Compact snapshot for the panel's live view."""
        with self._lock:
            record = self._current
            current = record.to_dict() if record else None
        return {
            "sample": self.sampler.latest(),
            "current": current,
            "sampler": self.sampler.stats(),
        }

    def errors(self, limit: int = 20) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        with self._lock:
            for record in reversed(self._history):
                for error in record.errors:
                    payload = error.to_dict()
                    payload["prompt_id"] = record.prompt_id
                    payload["run_index"] = record.index
                    payload["workflow"] = record.workflow
                    out.append(payload)
                    if len(out) >= limit:
                        return out
            if self._current is not None:
                for error in self._current.errors:
                    payload = error.to_dict()
                    payload["prompt_id"] = self._current.prompt_id
                    payload["run_index"] = self._current.index
                    payload["workflow"] = self._current.workflow
                    out.append(payload)
        return out[:limit]

    def clear(self) -> None:
        with self._lock:
            self._history = []
            self._counter = 0

    def delete(self, prompt_id: str) -> bool:
        with self._lock:
            before = len(self._history)
            self._history = [r for r in self._history if r.prompt_id != prompt_id]
            return len(self._history) != before

    def attach_analysis(self, prompt_id: str, analysis: Dict[str, Any]) -> bool:
        with self._lock:
            record = self.get(prompt_id)
            if record is None:
                return False
            record.analysis = analysis
        self._persist(record, force=True)
        return True

    # -- persistence ------------------------------------------------------
    def _persist(self, record: RunRecord, force: bool = False) -> None:
        if not force and not self.config.get("save_runs", True):
            return
        try:
            # Keep a hardware timeline only when the run failed - it is the
            # expensive part of the payload and only useful for diagnosis.
            keep_timeline = bool(record.errors)
            if keep_timeline:
                record.timeline = self._timeline_for(record)
            payload = record.to_dict(include_timeline=keep_timeline)
            name = (
                f"{time.strftime('%Y%m%d-%H%M%S', time.localtime(record.started_wall))}"
                f"_{_safe_name(record.workflow, 32)}"
                f"_{record.prompt_id[:8]}"
                f"_{record.status}.json"
            )
            path = os.path.join(self.log_dir, name)
            _atomic_write_json(path, payload)
            record.saved_path = path
            self._prune_logs()
        except Exception:
            pass

    def _timeline_for(self, record: RunRecord) -> List[Dict[str, Any]]:
        """Downsampled hardware trace covering the run, for failure reports."""
        if record.nodes:
            t_from = min((n.t_start or 0.0) for n in record.nodes)
            t_to = max((n.t_end or 0.0) for n in record.nodes)
            samples = self.sampler.window(t_from, t_to)
        else:
            samples = self.sampler.series(300)
        if not samples:
            samples = self.sampler.series(120)
        # Cap the trace length so saved artifacts stay small.
        if len(samples) > 240:
            step = max(1, len(samples) // 240)
            samples = samples[::step]
        out = []
        for sample in samples:
            gpu = sample.get("gpu") or {}
            out.append(
                {
                    "wall": sample.get("wall"),
                    "gpu_util": gpu.get("util_percent"),
                    "vram_mb": gpu.get("mem_used_mb"),
                    "cpu": sample.get("cpu_percent"),
                    "ram_mb": sample.get("ram_used_mb"),
                    "disk_read": sample.get("disk_read_mb_s"),
                    "disk_write": sample.get("disk_write_mb_s"),
                }
            )
        return out

    def _prune_logs(self) -> None:
        cap = int(self.config.get("max_saved_runs") or 100)
        try:
            files = [
                os.path.join(self.log_dir, f)
                for f in os.listdir(self.log_dir)
                if f.endswith(".json")
            ]
            if len(files) <= cap:
                return
            files.sort(key=lambda p: os.path.getmtime(p))
            for path in files[: len(files) - cap]:
                try:
                    os.remove(path)
                except OSError:
                    pass
        except Exception:
            pass

    def save_error_artifact(self, error: RunError, record: Optional[RunRecord]) -> Optional[str]:
        if not self.config.get("save_error_artifacts", True):
            return None
        try:
            payload = {
                "captured_at": _iso(error.t),
                "error": error.to_dict(),
                "run": record.to_dict(include_timeline=True) if record else None,
            }
            name = (
                f"error_{time.strftime('%Y%m%d-%H%M%S', time.localtime(error.t))}"
                f"_{(error.node_type or 'unknown')[:40]}.json"
            )
            path = os.path.join(self.log_dir, _safe_name(name, 120))
            _atomic_write_json(path, payload)
            return path
        except Exception:
            return None


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------
def _history_cap(config) -> int:
    try:
        return max(1, int(config.get("history_runs") or 20))
    except Exception:
        return 20


def _window_from_nodes(sampler, record: RunRecord) -> List[Dict[str, Any]]:
    if not record.nodes:
        return sampler.series(300)
    t_from = min((n.t_start or 0.0) for n in record.nodes)
    t_to = max((n.t_end or 0.0) for n in record.nodes)
    samples = sampler.window(t_from, t_to)
    return samples or sampler.series(300)


def _traceback_lines(raw: Any) -> List[str]:
    if raw is None:
        return []
    if isinstance(raw, str):
        lines = raw.splitlines()
    elif isinstance(raw, (list, tuple)):
        lines = []
        for item in raw:
            if isinstance(item, str):
                lines.extend(item.splitlines())
            else:
                lines.append(str(item))
    else:
        lines = [str(raw)]
    lines = [line.rstrip() for line in lines if line and line.strip()]
    if len(lines) > MAX_TRACEBACK_LINES:
        lines = ["... (truncated) ..."] + lines[-MAX_TRACEBACK_LINES:]
    return lines


def _shrink(value: Any, depth: int = 0) -> Any:
    """Make arbitrary node inputs JSON-safe and small."""
    if depth > 3:
        return "..."
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return value if len(value) <= 400 else value[:400] + "..."
    if isinstance(value, (list, tuple)):
        return [_shrink(v, depth + 1) for v in list(value)[:20]]
    if isinstance(value, dict):
        return {str(k): _shrink(v, depth + 1) for k, v in list(value.items())[:30]}
    return repr(value)[:200]


def _workflow_name(extra_data: Optional[Dict[str, Any]]) -> str:
    if not isinstance(extra_data, dict):
        return ""
    info = extra_data.get("extra_pnginfo")
    if isinstance(info, dict):
        workflow = info.get("workflow")
        if isinstance(workflow, dict):
            for key in ("name", "title", "id"):
                value = workflow.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()[:120]
    return ""


def _titles_from(
    extra_data: Optional[Dict[str, Any]], prompt: Optional[Dict[str, Any]]
) -> tuple:
    """Build (id -> display title, id -> class_type) maps for a run.

    ``class_type`` comes from the prompt dict, which is the authoritative source.
    Titles come from the frontend workflow JSON when available, because that is
    where a user's renamed node titles live (the prompt only carries class_type).
    Falls back to class_type when the workflow payload is absent, which is the
    case for prompts submitted through the HTTP API rather than the UI.
    """
    types: Dict[str, str] = {}
    if isinstance(prompt, dict):
        for node_id, node in prompt.items():
            if isinstance(node, dict):
                class_type = node.get("class_type")
                if class_type:
                    types[str(node_id)] = str(class_type)

    titles: Dict[str, str] = {}
    if isinstance(extra_data, dict):
        info = extra_data.get("extra_pnginfo")
        workflow = info.get("workflow") if isinstance(info, dict) else None
        if isinstance(workflow, dict):
            _collect_titles(workflow.get("nodes"), titles)
            _collect_titles(_subgraph_nodes(workflow), titles)

    # class_type is the fallback title for anything the workflow did not name.
    for node_id, class_type in types.items():
        titles.setdefault(node_id, class_type)
    return titles, types


def _subgraph_nodes(workflow: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Definitions of subgraphs carry their own node lists."""
    collected: List[Dict[str, Any]] = []
    for key in ("definitions", "subgraphs"):
        container = workflow.get(key)
        if isinstance(container, dict):
            for definition in container.values():
                if isinstance(definition, dict):
                    collected.extend(definition.get("nodes") or [])
        elif isinstance(container, list):
            for definition in container:
                if isinstance(definition, dict):
                    collected.extend(definition.get("nodes") or [])
    return collected


def _collect_titles(nodes: Any, out: Dict[str, str]) -> None:
    """Index id -> title, descending into subgraph definitions.

    Flattened subgraphs execute their inner nodes under the inner node's own id,
    so those ids must be indexed too or they fall back to the class type.
    """
    if not isinstance(nodes, list):
        return
    for node in nodes:
        if not isinstance(node, dict):
            continue
        node_id = node.get("id")
        if node_id is not None:
            title = node.get("title") or node.get("type") or ""
            if title:
                out.setdefault(str(node_id), str(title)[:120])
        # Packed subgraph definitions keep their nodes here.
        for key in ("definitions", "subgraphs"):
            container = node.get(key)
            if isinstance(container, dict):
                for definition in container.values():
                    if isinstance(definition, dict):
                        _collect_titles(definition.get("nodes"), out)
            elif isinstance(container, list):
                for definition in container:
                    if isinstance(definition, dict):
                        _collect_titles(definition.get("nodes"), out)


def _atomic_write_json(path: str, payload: Any) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2, default=str)
    os.replace(tmp, path)
