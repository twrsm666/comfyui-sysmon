"""Persisted plugin configuration.

Settings live in ``sysmon_config.json`` next to the plugin so the whole folder
stays self-contained (easy to back up, easy to delete).  No ComfyUI imports
here on purpose: this must work in a bare Python interpreter for testing.
"""

from __future__ import annotations

import json
import os
import threading
from typing import Any, Dict

# --- LLM provider presets -------------------------------------------------
# base_url values are the API roots; the full path is base_url + endpoint.
PROVIDER_PRESETS: Dict[str, Dict[str, Any]] = {
    "deepseek": {
        "label": "DeepSeek",
        "base_url": "https://api.deepseek.com",
        "endpoint": "/chat/completions",
        "model": "deepseek-chat",
        "models": ["deepseek-chat", "deepseek-reasoner"],
        "key_env": "DEEPSEEK_API_KEY",
        "key_url": "https://platform.deepseek.com/api_keys",
    },
    "openai_compatible": {
        "label": "OpenAI Compatible (custom)",
        "base_url": "https://api.openai.com/v1",
        "endpoint": "/chat/completions",
        "model": "gpt-4o-mini",
        "models": [],
        "key_env": "OPENAI_API_KEY",
        "key_url": "",
    },
}

DEFAULT_CONFIG: Dict[str, Any] = {
    # --- sampling ---
    "enabled": True,
    "sample_interval_ms": 1000,      # 1 Hz keeps overhead ~0.1% of one core
    "sample_interval_active_ms": 500,  # faster while a prompt is executing
    "history_size": 900,             # rolling samples kept in memory
    "gpu_index": None,               # None = auto (first NVIDIA GPU)

    # --- disk ---
    # Which path to report throughput for. "" = sum of all physical disks.
    "disk_path": "",

    # --- persistence ---
    "save_runs": True,               # write per-run JSON to ./logs
    "max_saved_runs": 100,           # JSON files kept in ./logs before pruning
    "history_runs": 20,              # runs kept in memory / shown in the panel
    "save_error_artifacts": True,    # write a detailed JSON per failure

    # --- thresholds used by the UI for colour coding ---
    "warn_vram_percent": 85.0,
    "crit_vram_percent": 95.0,
    "warn_ram_percent": 85.0,
    "crit_ram_percent": 95.0,
    "warn_gpu_percent": 0.0,         # 0 disables GPU-util warnings
    "crit_gpu_percent": 0.0,

    # --- LLM ---
    # On by default: supplying an API key is itself the consent decision.
    # Defaulting to off silently gated the feature behind an opt-in the user
    # had no reason to look for.
    "llm_enabled": True,
    "llm_provider": "deepseek",
    "llm_base_url": "",
    "llm_api_key": "",
    "llm_model": "",
    "llm_timeout_s": 120,
    "llm_max_tokens": 1500,
    "llm_temperature": 0.2,
    "llm_language": "zh",            # zh | en - language of the advice
}


def _deep_merge(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def _is_masked_key(candidate: Any, stored: Any) -> bool:
    """True when ``candidate`` is the display mask of the stored key."""
    if candidate is None or candidate == "":
        return True
    if not isinstance(candidate, str):
        return False
    if candidate == "***":
        return True
    # Format produced by Config.public(): "<first4>...<last4>".
    if "..." in candidate and stored and isinstance(stored, str):
        parts = candidate.split("...")
        if len(parts) == 2 and (stored.startswith(parts[0]) or stored.endswith(parts[1])):
            return True
    return False


class Config:
    """Thread-safe, lazily loaded JSON config with atomic writes."""

    def __init__(self, path: str):
        self.path = path
        self._lock = threading.RLock()
        self._data: Dict[str, Any] = dict(DEFAULT_CONFIG)
        self.load()

    # -- io ---------------------------------------------------------------
    def load(self) -> Dict[str, Any]:
        with self._lock:
            if os.path.isfile(self.path):
                try:
                    with open(self.path, "r", encoding="utf-8") as fh:
                        on_disk = json.load(fh)
                    if isinstance(on_disk, dict):
                        self._data = _deep_merge(DEFAULT_CONFIG, on_disk)
                except Exception:
                    # A corrupt config must never stop ComfyUI from booting.
                    self._data = dict(DEFAULT_CONFIG)
            return dict(self._data)

    def save(self) -> None:
        with self._lock:
            tmp = self.path + ".tmp"
            try:
                with open(tmp, "w", encoding="utf-8") as fh:
                    json.dump(self._data, fh, indent=2, ensure_ascii=False)
                os.replace(tmp, self.path)
            except Exception:
                if os.path.exists(tmp):
                    try:
                        os.remove(tmp)
                    except OSError:
                        pass

    # -- access -----------------------------------------------------------
    def all(self) -> Dict[str, Any]:
        with self._lock:
            return dict(self._data)

    def get(self, key: str, default: Any = None) -> Any:
        with self._lock:
            return self._data.get(key, DEFAULT_CONFIG.get(key, default))

    def update(self, patch: Dict[str, Any]) -> Dict[str, Any]:
        """Merge ``patch`` in and persist. Unknown keys are kept (forward compat).

        A masked API key is refused here rather than at the HTTP layer: the
        browser is sent a masked value by :meth:`public`, and if that value were
        ever written back it would silently destroy the real key. Guarding in
        the config object makes that impossible for any caller.
        """
        with self._lock:
            patch = dict(patch or {})
            if "llm_api_key" in patch:
                candidate = patch.get("llm_api_key")
                if _is_masked_key(candidate, self._data.get("llm_api_key")):
                    patch.pop("llm_api_key")
            self._data = _deep_merge(self._data, patch)
            self.save()
            return dict(self._data)

    # -- helpers ----------------------------------------------------------
    def provider(self) -> Dict[str, Any]:
        name = self.get("llm_provider") or "deepseek"
        return PROVIDER_PRESETS.get(name, PROVIDER_PRESETS["deepseek"])

    def resolved_llm(self) -> Dict[str, Any]:
        """Effective LLM settings: user overrides on top of the provider preset."""
        preset = self.provider()
        key = (self.get("llm_api_key") or "").strip()
        if not key and preset.get("key_env"):
            key = (os.environ.get(preset["key_env"]) or "").strip()
        return {
            "provider": self.get("llm_provider"),
            "base_url": (self.get("llm_base_url") or "").strip() or preset["base_url"],
            "endpoint": preset.get("endpoint", "/chat/completions"),
            "model": (self.get("llm_model") or "").strip() or preset["model"],
            "api_key": key,
            "timeout_s": int(self.get("llm_timeout_s") or 120),
            "max_tokens": int(self.get("llm_max_tokens") or 1500),
            "temperature": float(self.get("llm_temperature") or 0.2),
            "language": self.get("llm_language") or "zh",
        }

    def public(self) -> Dict[str, Any]:
        """Config with the API key masked, safe to hand to the browser."""
        data = self.all()
        raw = data.get("llm_api_key") or ""
        if raw:
            data["llm_api_key"] = "***" if len(raw) <= 8 else f"{raw[:4]}...{raw[-4:]}"
        data["llm_api_key_set"] = bool(raw) or bool(
            os.environ.get(self.provider().get("key_env") or "", "")
        )
        data["provider_presets"] = PROVIDER_PRESETS
        return data
