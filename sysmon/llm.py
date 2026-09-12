"""Optional online-LLM diagnosis of a run or a failure.

Uses only the standard library for HTTP so the plugin needs no extra
dependencies, and speaks the OpenAI ``/chat/completions`` wire format which
both DeepSeek and any OpenAI-compatible gateway implement.

Privacy: this module never sends images, prompt text or model outputs.  It
sends the exception, the run's hardware peaks, and the graph's node types.
:func:`build_context` is the single place that decides what leaves the machine,
so it is easy to audit.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional

from .metrics import METRIC_DEFS

# Metrics that matter for diagnosis, in the order we present them.
_CONTEXT_METRICS = [
    "gpu_util",
    "vram_used",
    "gpu_temp",
    "gpu_power",
    "cpu",
    "ram_used",
    "disk_read",
    "disk_write",
]

METRIC_LABELS_EN = {
    "gpu_util": "GPU utilization",
    "vram_used": "VRAM used",
    "gpu_temp": "GPU temperature",
    "gpu_power": "GPU power",
    "cpu": "CPU utilization",
    "ram_used": "System RAM used",
    "disk_read": "Disk read",
    "disk_write": "Disk write",
}

SYSTEM_PROMPT_ZH = (
    "你是一名资深的 Stable Diffusion / ComfyUI 部署与性能诊断专家。"
    "你会收到一次 ComfyUI 工作流运行的硬件指标快照（GPU/显存/CPU/内存/磁盘峰值，"
    "以及每个节点的耗时和峰值归属），如果这次运行失败，还会附带报错堆栈。\n"
    "请严格基于给出的数据作答，不要编造数据中不存在的信息。\n"
    "如果数据不足以定位原因，请明确说明还需要哪些信息。\n\n"
    "输出使用简体中文，用 Markdown 组织，必须包含以下小节：\n"
    "## 结论\n（一句话说明这次运行是否正常，或最可能的失败原因）\n"
    "## 瓶颈分析\n（哪个节点/环节是瓶颈，依据是哪些指标；显存是否吃紧、是否触发了换页或内存回收）\n"
    "## 优化建议\n（按收益从高到低排序，每条给出具体可执行的参数或做法，例如降低分辨率/批大小、"
    "调整 --lowvram/--novram、更换精度、增大虚拟内存、把模型放到更快的磁盘等）\n"
    "## 风险提示\n（继续这样跑可能出现的问题，例如 OOM、硬盘写入放大、显存碎片）\n"
    "保持简洁，总长度控制在 500 字以内，不要复述原始数据。"
)

SYSTEM_PROMPT_EN = (
    "You are an expert in Stable Diffusion / ComfyUI deployment and performance "
    "diagnosis. You receive a hardware snapshot of one ComfyUI workflow run "
    "(peak GPU/VRAM/CPU/RAM/disk plus per-node durations and peak attribution), "
    "and, if the run failed, the exception traceback.\n"
    "Answer strictly from the supplied data; never invent values. If the data is "
    "insufficient, say exactly what else is needed.\n\n"
    "Reply in English using Markdown with these sections:\n"
    "## Verdict\n(one line: was the run healthy, or the most likely cause of failure)\n"
    "## Bottleneck\n(which node/stage is the bottleneck and which metrics prove it; "
    "was VRAM tight, did it spill to shared memory or trigger RAM cache eviction)\n"
    "## Recommendations\n(ordered by impact, each with concrete parameter or action: "
    "resolution/batch size, --lowvram/--novram, precision, pagefile size, faster disk, ...)\n"
    "## Risks\n(what will break if nothing changes: OOM, write amplification, fragmentation)\n"
    "Keep it under 400 words and do not restate the raw data."
)


# --------------------------------------------------------------------------
# Context building
# --------------------------------------------------------------------------
def build_context(
    run: Optional[Dict[str, Any]],
    error: Optional[Dict[str, Any]] = None,
    *,
    include_graph: bool = True,
    max_nodes: int = 60,
    language: str = "zh",
) -> Dict[str, Any]:
    """Assemble the exact payload that will be sent to the model.

    Returned unredacted so the UI can show the user precisely what is sent.
    """
    labels = METRIC_LABELS_EN if language == "en" else {
        k: v for k, v in _labels_zh().items()
    }
    context: Dict[str, Any] = {
        "comfyui_run": {},
        "hardware_peaks": {},
        "slowest_nodes": [],
        "memory_pressure": {},
        "error": None,
    }

    if run:
        context["comfyui_run"] = {
            "status": run.get("status"),
            "duration_s": run.get("duration_s"),
            "started": run.get("started_iso"),
            "workflow_name": run.get("workflow") or "(unsaved workflow)",
            "total_nodes": run.get("node_count"),
            "executed_nodes": run.get("executed_count"),
            "cached_nodes": run.get("cached_count"),
            "gpu": run.get("gpu_name"),
            "metrics_source": run.get("gpu_backend"),
            "sampling_interval_ms": run.get("sample_interval_ms"),
        }

        peaks = run.get("peaks") or {}
        hardware: Dict[str, Any] = {}
        for key in _CONTEXT_METRICS:
            entry = peaks.get(key)
            if not entry:
                continue
            label = labels.get(key, key)
            hardware[label] = f"{entry.get('value')} {entry.get('unit')}".strip()
            if entry.get("percent") is not None:
                # Express capacity usage so the model can reason about headroom.
                hardware[label + " (% of capacity)"] = entry["percent"]
        context["hardware_peaks"] = hardware

        # Peak attribution: which node caused each run-level peak.
        attribution: Dict[str, Any] = {}
        for key in _CONTEXT_METRICS:
            owner = _peak_owner(run.get("nodes") or [], key)
            if owner:
                attribution[labels.get(key, key)] = {
                    "node": owner["title"],
                    "node_type": owner["class_type"],
                    "value": owner["value"],
                    "unit": owner["unit"],
                }
        if attribution:
            context["peak_attribution"] = attribution

        nodes = [n for n in (run.get("nodes") or []) if not n.get("cached")]
        nodes.sort(key=lambda n: n.get("duration_s") or 0.0, reverse=True)
        context["slowest_nodes"] = [
            {
                "node": n.get("title") or f"#{n.get('node_id')}",
                "node_type": n.get("class_type"),
                "seconds": n.get("duration_s"),
                "peak_vram_mb": (n.get("peaks") or {}).get("vram_used", {}).get("value"),
                "peak_gpu_percent": (n.get("peaks") or {}).get("gpu_util", {}).get("value"),
                "peak_ram_mb": (n.get("peaks") or {}).get("ram_used", {}).get("value"),
            }
            for n in nodes[:12]
        ]

        context["memory_pressure"] = _memory_pressure(run, peaks)

        if include_graph and run.get("nodes"):
            context["workflow_graph"] = [
                {
                    "id": n.get("node_id"),
                    "title": n.get("title"),
                    "type": n.get("class_type"),
                    "seconds": n.get("duration_s"),
                }
                for n in (run.get("nodes") or [])[:max_nodes]
            ]

    if error:
        context["error"] = {
            "kind": error.get("kind"),
            "failed_node": error.get("node_title") or error.get("node_type"),
            "failed_node_type": error.get("node_type"),
            "exception_type": error.get("exception_type"),
            "exception_message": error.get("exception_message"),
            "traceback_tail": (error.get("traceback") or [])[-25:],
            "node_inputs_summary": error.get("current_inputs"),
        }

    return context


def _labels_zh() -> Dict[str, str]:
    return {key: label for key, label, _p, _u, _w in METRIC_DEFS}


def _peak_owner(nodes: List[Dict[str, Any]], metric: str) -> Optional[Dict[str, Any]]:
    best: Optional[Dict[str, Any]] = None
    for node in nodes:
        entry = (node.get("peaks") or {}).get(metric)
        if not entry:
            continue
        value = entry.get("value")
        if value is None:
            continue
        if best is None or value > best["value"]:
            best = {
                "title": node.get("title") or f"#{node.get('node_id')}",
                "class_type": node.get("class_type"),
                "value": value,
                "unit": entry.get("unit"),
            }
    return best


def _memory_pressure(run: Dict[str, Any], peaks: Dict[str, Any]) -> Dict[str, Any]:
    """Flag the situations that actually cause ComfyUI trouble."""
    out: Dict[str, Any] = {}
    vram = peaks.get("vram_used") or {}
    ram = peaks.get("ram_used") or {}
    if vram.get("percent") is not None:
        out["vram_peak_percent"] = vram["percent"]
        if vram["percent"] >= 95:
            out["vram_status"] = "critical - very close to VRAM ceiling"
        elif vram["percent"] >= 85:
            out["vram_status"] = "high - little headroom left"
        else:
            out["vram_status"] = "ok"
    if ram.get("percent") is not None:
        out["ram_peak_percent"] = ram["percent"]
        if ram["percent"] >= 95:
            out["ram_status"] = "critical - system may start paging"
        elif ram["percent"] >= 85:
            out["ram_status"] = "high - pagefile may be in use"
        else:
            out["ram_status"] = "ok"

    # Nodes that sat within 5% of the VRAM ceiling are the OOM suspects.
    suspects = []
    for node in run.get("nodes") or []:
        entry = (node.get("peaks") or {}).get("vram_used")
        if not entry or entry.get("percent") is None:
            continue
        if entry["percent"] >= 90:
            suspects.append(
                {
                    "node": node.get("title") or f"#{node.get('node_id')}",
                    "type": node.get("class_type"),
                    "vram_percent": entry["percent"],
                }
            )
    if suspects:
        suspects.sort(key=lambda s: s["vram_percent"], reverse=True)
        out["nodes_near_vram_limit"] = suspects[:8]
    return out


# --------------------------------------------------------------------------
# Prompt assembly
# --------------------------------------------------------------------------
def build_messages(
    context: Dict[str, Any],
    language: str = "zh",
    extra_question: str = "",
    history: Optional[List[Dict[str, str]]] = None,
) -> List[Dict[str, str]]:
    system = SYSTEM_PROMPT_EN if language == "en" else SYSTEM_PROMPT_ZH
    messages = [{"role": "system", "content": system}]
    for turn in history or []:
        role = turn.get("role")
        content = turn.get("content")
        if role in ("user", "assistant") and content:
            messages.append({"role": role, "content": str(content)[:8000]})
    intro = (
        "Here is the run snapshot as JSON:\n\n"
        if language == "en"
        else "以下是本次运行的快照数据（JSON）：\n\n"
    )
    messages.append(
        {
            "role": "user",
            "content": intro
            + "```json\n"
            + json.dumps(context, ensure_ascii=False, indent=1, default=str)
            + "\n```",
        }
    )
    if extra_question.strip():
        label = "Additional question: " if language == "en" else "补充问题："
        messages.append({"role": "user", "content": label + extra_question.strip()[:2000]})
    return messages


# --------------------------------------------------------------------------
# Transport
# --------------------------------------------------------------------------
class LLMError(RuntimeError):
    """Raised with a message safe to show directly in the UI."""


def analyze(
    settings: Dict[str, Any],
    context: Dict[str, Any],
    extra_question: str = "",
    history: Optional[List[Dict[str, str]]] = None,
) -> Dict[str, Any]:
    """Send ``context`` to the configured chat-completions endpoint."""
    api_key = (settings.get("api_key") or "").strip()
    if not api_key:
        raise LLMError(
            "未配置 API Key。请在面板设置里填入 DeepSeek 或 OpenAI 兼容接口的 Key。"
            if settings.get("language") != "en"
            else "No API key configured. Add one in the panel settings."
        )

    base_url = (settings.get("base_url") or "").rstrip("/")
    endpoint = settings.get("endpoint") or "/chat/completions"
    url = base_url + (endpoint if endpoint.startswith("/") else "/" + endpoint)

    language = settings.get("language") or "zh"
    payload = {
        "model": settings.get("model"),
        "messages": build_messages(context, language, extra_question, history),
        "temperature": settings.get("temperature", 0.2),
        "max_tokens": settings.get("max_tokens", 1500),
        "stream": False,
    }
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
            "Accept": "application/json",
            "User-Agent": "comfyui-sysmon/1.0",
        },
    )
    timeout = int(settings.get("timeout_s") or 120)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8", "replace")[:600]
        except Exception:
            pass
        raise LLMError(_http_hint(exc.code, detail)) from exc
    except urllib.error.URLError as exc:
        raise LLMError(f"网络请求失败：{exc.reason}") from exc
    except TimeoutError as exc:
        raise LLMError(f"请求超时（{timeout}s）。") from exc

    try:
        data = json.loads(raw)
    except ValueError as exc:
        raise LLMError(f"接口返回的不是 JSON：{raw[:300]}") from exc

    if isinstance(data, dict) and data.get("error"):
        err = data["error"]
        message = err.get("message") if isinstance(err, dict) else str(err)
        raise LLMError(f"接口返回错误：{message}")

    text = _extract_text(data)
    if not text:
        raise LLMError(f"接口没有返回内容：{raw[:300]}")

    usage = data.get("usage") if isinstance(data, dict) else None
    return {
        "text": text,
        "model": (data.get("model") if isinstance(data, dict) else None)
        or settings.get("model"),
        "usage": usage,
        "provider": settings.get("provider"),
        "base_url": base_url,
    }


def _extract_text(data: Any) -> str:
    if not isinstance(data, dict):
        return ""
    choices = data.get("choices")
    if not isinstance(choices, list) or not choices:
        return ""
    first = choices[0]
    if not isinstance(first, dict):
        return ""
    message = first.get("message")
    if isinstance(message, dict):
        content = message.get("content")
        if isinstance(content, str):
            return content.strip()
        if isinstance(content, list):  # some gateways return content parts
            parts = [
                p.get("text", "")
                for p in content
                if isinstance(p, dict) and p.get("type") in (None, "text")
            ]
            return "\n".join(p for p in parts if p).strip()
    # Fallbacks for non-standard gateways.
    if isinstance(first.get("text"), str):
        return first["text"].strip()
    return ""


def _http_hint(code: int, detail: str) -> str:
    hints = {
        400: "请求被拒绝，通常是模型名不对或参数不被支持。",
        401: "API Key 无效或已过期（401）。",
        402: "账户余额不足（402）。",
        403: "无权访问该模型（403）。",
        404: "接口地址不对（404），请检查 Base URL，DeepSeek 应为 https://api.deepseek.com。",
        429: "触发限流或余额不足（429），请稍后重试。",
        500: "服务端错误（500），稍后重试。",
        502: "网关错误（502）。",
        503: "服务暂不可用（503）。",
    }
    hint = hints.get(code, f"HTTP {code}。")
    return f"{hint}\n{detail}" if detail else hint


def test_connection(settings: Dict[str, Any]) -> Dict[str, Any]:
    """Cheap round-trip used by the 'test' button in settings."""
    context = {
        "comfyui_run": {"status": "connection_test"},
        "note": "This is a connectivity test from the ComfyUI System Monitor plugin.",
    }
    result = analyze(settings, context, extra_question="Reply with the single word: OK")
    return {"ok": True, "model": result.get("model"), "reply": result.get("text", "")[:200]}
