"""ComfyUI integration: hooks that feed the run store.

Where per-node events actually come from (verified against ComfyUI 0.32.0 source
and against a live server):

``comfy_execution.progress``
    In 0.32 the executor drives nodes through a ``ProgressRegistry`` and calls
    ``start_progress`` / ``finish_progress`` per node, notifying every
    registered ``ProgressHandler``.  This is the sanctioned extension point and
    the reliable per-node signal.

    Two traps had to be worked around, both verified by instrumenting a running
    server:
      1. ``execution.py`` does ``from comfy_execution.progress import
         add_progress_handler``, binding the name locally.  Patching the *source*
         module has no effect on what the executor calls.
      2. ``execute_async`` calls ``reset_progress_state()`` - which *replaces*
         the registry - and only then registers its own handler.  A handler
         registered before that point is silently discarded.
    So the registry is captured via ``reset_progress_state`` and the handler is
    attached when core registers its own.

``PromptExecutor.add_message``
    run-level lifecycle - ``execution_start``, ``execution_cached``,
    ``execution_success``, ``execution_error``, ``execution_interrupted``.

``PromptExecutor.execute_async``
    brackets a whole run.

Legacy fallback
    Older ComfyUI builds emit ``"executing"`` / ``"executed"`` through
    ``PromptServer.send_sync`` instead.  If the progress API is unavailable the
    node hooks are taken from there, so the plugin still works on older cores.

Every hook is wrapped so a bug in *this* plugin can never abort a user's
generation: exceptions are swallowed and reported once.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Dict, Optional

logger = logging.getLogger("sysmon")

# Run-level lifecycle, delivered through PromptExecutor.add_message.
_EVENT_EXECUTION_START = "execution_start"
_EVENT_CACHED = "execution_cached"
_EVENT_SUCCESS = "execution_success"
_EVENT_ERROR = "execution_error"
_EVENT_INTERRUPTED = "execution_interrupted"

# Legacy node-level lifecycle, delivered through PromptServer.send_sync.
_EVENT_NODE_START = "executing"
_EVENT_NODE_DONE = "executed"

_PROGRESS_HANDLER_NAME = "sysmon"


def _debug(message: str) -> None:
    if os.environ.get("SYSMON_DEBUG"):
        print(f"[sysmon-debug] {message}", flush=True)


class SysmonProgressHandler:
    """ProgressHandler that turns node state changes into store calls.

    Duck-typed rather than subclassing ``ProgressHandler`` so this module stays
    importable without ComfyUI present.
    """

    def __init__(self, hooks: "ComfyHooks"):
        self.hooks = hooks
        self.name = _PROGRESS_HANDLER_NAME
        self.enabled = True
        self.registry = None

    # -- ProgressHandler interface ---------------------------------------
    def set_registry(self, registry) -> None:
        self.registry = registry

    def enable(self) -> None:
        self.enabled = True

    def disable(self) -> None:
        self.enabled = False

    def reset(self) -> None:
        # The registry is rebuilt per prompt; nothing to release.
        pass

    def start_handler(self, node_id, state, prompt_id) -> None:
        if not self.enabled:
            return
        try:
            self.hooks.on_node_start(str(node_id))
        except Exception as exc:
            self.hooks._report_once("progress.start", exc)

    def update_handler(self, node_id, value, max_value, state, prompt_id, image=None) -> None:
        # Sampling is time based; intermediate progress values are not needed.
        return

    def finish_handler(self, node_id, state, prompt_id) -> None:
        if not self.enabled:
            return
        try:
            self.hooks.on_node_finish(str(node_id))
        except Exception as exc:
            self.hooks._report_once("progress.finish", exc)


class ComfyHooks:
    """Installs and owns the monkeypatches; ``uninstall()`` restores originals."""

    def __init__(self, store, sampler, config):
        self.store = store
        self.sampler = sampler
        self.config = config
        self._installed = False
        self._executor_cls = None
        self._server_cls = None
        self._originals: Dict[str, Any] = {}
        self._errored = False
        self._open_node: Optional[str] = None
        self._node_starts = 0
        self._node_ends = 0
        self._progress_bridge = False
        self._legacy_send_sync = False
        self._pending_error_artifact = None
        self.progress_handler = SysmonProgressHandler(self)

    # -- install ----------------------------------------------------------
    def install(self) -> bool:
        if self._installed:
            return True
        try:
            from execution import PromptExecutor  # type: ignore
        except Exception as exc:  # ComfyUI layout changed or running headless
            logger.warning("[sysmon] execution hooks unavailable: %s", exc)
            return False

        self._executor_cls = PromptExecutor
        try:
            self._originals["execute_async"] = PromptExecutor.execute_async
            self._originals["add_message"] = PromptExecutor.add_message
        except AttributeError as exc:
            logger.warning("[sysmon] ComfyUI executor shape unexpected: %s", exc)
            return False

        hooks = self

        async def execute_async(self, prompt, prompt_id, extra_data={}, execute_outputs=[]):
            return await hooks._run_wrapper(
                hooks._originals["execute_async"],
                self,
                prompt,
                prompt_id,
                extra_data,
                execute_outputs,
            )

        def add_message(self, event, data: dict, broadcast: bool):
            try:
                hooks._on_run_message(event, data)
            except Exception as exc:  # never break the executor
                hooks._report_once("add_message", exc)
            return hooks._originals["add_message"](self, event, data, broadcast)

        PromptExecutor.execute_async = execute_async
        PromptExecutor.add_message = add_message

        # Prefer the progress registry; fall back to websocket messages.
        if not self._install_progress_bridge():
            self._install_legacy_node_hooks()

        self._installed = True
        logger.info(
            "[sysmon] hooks installed (executor lifecycle + %s)",
            "progress registry" if self._progress_bridge else "send_sync fallback",
        )
        return True

    def _install_progress_bridge(self) -> bool:
        """Attach to the per-prompt progress registry when core builds it."""
        try:
            from comfy_execution import progress as progress_module  # type: ignore
        except Exception:
            return False
        if not hasattr(progress_module, "ProgressRegistry"):
            return False
        try:
            import execution  # type: ignore
        except Exception:
            return False

        needed = ("reset_progress_state", "add_progress_handler")
        for name in needed:
            if not hasattr(execution, name) or not hasattr(progress_module, name):
                return False

        hooks = self
        # Names the executor actually calls (it imported them into its module).
        self._originals["exec_reset_progress_state"] = execution.reset_progress_state
        self._originals["exec_add_progress_handler"] = execution.add_progress_handler

        def reset_progress_state(prompt_id, dynprompt):
            # The registry is created here; core registers its handler next.
            result = hooks._originals["exec_reset_progress_state"](prompt_id, dynprompt)
            try:
                hooks.progress_handler.set_registry(progress_module.get_progress_state())
            except Exception as exc:
                hooks._report_once("reset_progress_state", exc)
            return result

        def add_progress_handler(handler):
            # Runs at the exact moment core attaches its WebUI handler, so our
            # handler lands on the same, freshly created registry.
            result = hooks._originals["exec_add_progress_handler"](handler)
            try:
                registry = progress_module.get_progress_state()
                registry.register_handler(hooks.progress_handler)
                hooks.progress_handler.set_registry(registry)
            except Exception as exc:
                hooks._report_once("add_progress_handler", exc)
            return result

        execution.reset_progress_state = reset_progress_state
        execution.add_progress_handler = add_progress_handler
        # Keep the source module consistent for any other caller.
        try:
            progress_module.add_progress_handler = add_progress_handler
        except Exception:
            pass
        self._progress_bridge = True
        return True

    def _install_legacy_node_hooks(self) -> None:
        """Older cores: take node boundaries from ``send_sync`` events."""
        try:
            from server import PromptServer  # type: ignore
        except Exception:
            return
        hooks = self
        self._server_cls = PromptServer
        self._originals["send_sync"] = PromptServer.send_sync

        def send_sync(self, event, data, sid=None):
            try:
                hooks._on_legacy_node_message(event, data)
            except Exception as exc:
                hooks._report_once("send_sync", exc)
            return hooks._originals["send_sync"](self, event, data, sid)

        PromptServer.send_sync = send_sync
        self._legacy_send_sync = True

    def uninstall(self) -> None:
        if not self._installed:
            return
        if self._executor_cls is not None:
            for name in ("execute_async", "add_message"):
                original = self._originals.get(name)
                if original is not None:
                    try:
                        setattr(self._executor_cls, name, original)
                    except Exception:
                        pass
        if self._progress_bridge:
            try:
                import execution  # type: ignore

                for name, key in (
                    ("reset_progress_state", "exec_reset_progress_state"),
                    ("add_progress_handler", "exec_add_progress_handler"),
                ):
                    original = self._originals.get(key)
                    if original is not None:
                        setattr(execution, name, original)
            except Exception:
                pass
        if self._legacy_send_sync and self._server_cls is not None:
            original = self._originals.get("send_sync")
            if original is not None:
                try:
                    self._server_cls.send_sync = original
                except Exception:
                    pass
        # The progress handler lives on a per-prompt registry that is discarded
        # when the next prompt starts, so disabling it is enough.
        try:
            self.progress_handler.disable()
        except Exception:
            pass
        self._installed = False
        self._originals.clear()

    @property
    def installed(self) -> bool:
        return self._installed

    def stats(self) -> Dict[str, Any]:
        return {
            "installed": self._installed,
            "mode": "progress" if self._progress_bridge else (
                "send_sync" if self._legacy_send_sync else "none"
            ),
            "node_starts": self._node_starts,
            "node_ends": self._node_ends,
            "open_node": self._open_node,
        }

    # -- node level -------------------------------------------------------
    def on_node_start(self, node_id: str) -> None:
        _debug(f"node start {node_id!r}")
        # A new node starting means the previous one is done: some nodes never
        # report progress at all, so closing here keeps attribution honest
        # instead of silently merging consecutive nodes.
        if self._open_node is not None and self._open_node != node_id:
            self.store.note_node_end(self._open_node)
        self._open_node = node_id
        self.store.note_node_start(node_id)
        self._node_starts += 1

    def on_node_finish(self, node_id: str) -> None:
        _debug(f"node finish {node_id!r}")
        self.store.note_node_end(node_id)
        self._node_ends += 1
        if self._open_node == node_id:
            self._open_node = None

    def _on_legacy_node_message(self, event: str, data: Any) -> None:
        if not isinstance(data, dict):
            return
        node = data.get("node")
        if node is None or node == "":
            return
        if event == _EVENT_NODE_START:
            self.on_node_start(str(node))
        elif event == _EVENT_NODE_DONE:
            self.on_node_finish(str(node))

    # -- run bracket ------------------------------------------------------
    async def _run_wrapper(self, original, executor, prompt, prompt_id, extra_data, execute_outputs):
        record = None
        status = "error"
        self._open_node = None
        self._pending_error_artifact = None
        try:
            # Capture the submission up front: node titles come from
            # extra_pnginfo, which is only reliable before execution starts.
            record = self.store.begin_run(prompt_id, prompt, extra_data)
        except Exception as exc:
            self._report_once("begin_run", exc)
        try:
            result = await original(executor, prompt, prompt_id, extra_data, execute_outputs)
            if record is not None:
                status = "success" if getattr(executor, "success", True) else "error"
            return result
        except Exception:
            status = "error"
            raise
        finally:
            try:
                if self._open_node is not None:
                    self.store.note_node_end(self._open_node)
                    self._open_node = None
            except Exception as exc:
                self._report_once("close_open_node", exc)
            try:
                self.store.after_run_cleanup(status, prompt_id)
            except Exception as exc:
                self._report_once("end_run", exc)
            # Now that the run is closed - with its duration, node list and
            # hardware timeline - write the detailed failure artifact. Doing
            # this earlier produced artifacts describing an unfinished run.
            try:
                error = self._pending_error_artifact
                if error is not None:
                    self.store.save_error_artifact(error, self.store.get(prompt_id))
                    self._pending_error_artifact = None
            except Exception as exc:
                self._report_once("save_error_artifact", exc)
            try:
                self.sampler.set_active(False)
            except Exception:
                pass

    # -- run-level events -------------------------------------------------
    def _on_run_message(self, event: str, data: Dict[str, Any]) -> None:
        if not isinstance(data, dict):
            return
        _debug(f"run event={event!r} keys={sorted(data.keys())}")
        prompt_id = str(data.get("prompt_id") or "")

        if event == _EVENT_EXECUTION_START:
            try:
                self.sampler.set_active(True)
            except Exception:
                pass
            return

        if event == _EVENT_CACHED:
            self.store.note_cached([str(n) for n in (data.get("nodes") or [])])
            return

        if event == _EVENT_ERROR:
            error = self.store.note_error(data, kind="error")
            if error is not None:
                # Remember it, but do NOT persist yet: at this instant the run
                # is still open, so the artifact would carry no duration, no
                # node list and an empty hardware timeline. The wrapper's
                # finally block writes the finished run instead.
                self._pending_error_artifact = error
            self._push("sysmon.error", {"prompt_id": prompt_id})
            return

        if event == _EVENT_INTERRUPTED:
            self.store.note_error(data, kind="interrupted")
            return

        if event == _EVENT_SUCCESS:
            self._finish_success(prompt_id)
            return

    def _finish_success(self, prompt_id: str) -> None:
        if self._open_node is not None:
            try:
                self.store.note_node_end(self._open_node)
            finally:
                self._open_node = None
        record = self.store.end_run("success", prompt_id)
        if record is not None:
            self._push("sysmon.run_complete", _run_event_payload(record))

    # -- push to the browser ---------------------------------------------
    def _push(self, event: str, payload: Dict[str, Any]) -> None:
        """Best-effort websocket broadcast; harmless when no client is attached."""
        try:
            from server import PromptServer  # type: ignore

            instance = getattr(PromptServer, "instance", None)
            if instance is None:
                return
            # Call the original to avoid re-entering our own legacy patch.
            original = self._originals.get("send_sync")
            if original is not None:
                original(instance, event, payload)
            else:
                instance.send_sync(event, payload)
        except Exception:
            pass

    def _report_once(self, where: str, exc: Exception) -> None:
        if not self._errored:
            self._errored = True
            logger.warning("[sysmon] hook error in %s (further errors suppressed): %s", where, exc)


def _run_event_payload(record) -> Dict[str, Any]:
    """Compact summary pushed to the UI when a run finishes."""
    return {
        "prompt_id": record.prompt_id,
        "index": record.index,
        "status": record.status,
        "duration_s": record.duration_s,
        "workflow": record.workflow,
        "peaks": record.peaks,
        "error_count": len(record.errors),
        "node_count": record.executed_count,
    }
