"""ComfyUI System Monitor - backend package.

Layers (bottom-up, each importable without ComfyUI present):

    config    - user settings persisted to JSON
    metrics   - hardware sampling (GPU / VRAM / CPU / RAM / disk)
    store     - run history, per-node peak attribution, error records
    llm       - optional online LLM analysis of a run
    api       - HTTP routes + websocket pushes
    hooks     - ComfyUI execution hooks that feed the store

``metrics``, ``config``, ``store`` and ``llm`` never import ``comfy_*`` or
``server`` so they can be unit-tested headlessly.
"""

__version__ = "1.0.0"

__all__ = ["__version__"]
