"""HTTP API exposed to the browser panel.

All routes live under ``/sysmon``.  They are registered lazily (see
``register_routes``) because custom nodes are imported *before* ComfyUI's
``server`` module is ready, and importing too early would create a second,
detached ``PromptServer`` instance.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import OrderedDict
from typing import Any, Dict, List, Optional

logger = logging.getLogger("sysmon")

ROUTE_PREFIX = "/sysmon"


class MonitorAPI:
    """Bundles the subsystems and serves them as JSON."""

    def __init__(self, config, sampler, store):
        self.config = config
        self.sampler = sampler
        self.store = store
        # Follow-up threads, keyed by prompt_id. Bounded: without a cap this
        # grows for the lifetime of the server process (one prompt_id per run,
        # never evicted), which is a slow leak in a long ComfyUI session.
        self._conversations: "OrderedDict[str, List[Dict[str, str]]]" = OrderedDict()
        self._conversations_max = 8
        self._routes_registered = False

    # -- conversation state ----------------------------------------------
    def _thread(self, prompt_id: str, create: bool = False) -> Optional[List[Dict[str, str]]]:
        """Return the follow-up thread for a run, evicting the oldest threads."""
        thread = self._conversations.get(prompt_id)
        if thread is None and create:
            thread = []
            self._conversations[prompt_id] = thread
        if thread is not None:
            self._conversations.move_to_end(prompt_id)
            while len(self._conversations) > self._conversations_max:
                self._conversations.popitem(last=False)
        return thread

    # -- registration -----------------------------------------------------
    def register(self) -> bool:
        if self._routes_registered:
            return True
        try:
            from aiohttp import web  # type: ignore
            from server import PromptServer  # type: ignore
        except Exception as exc:
            logger.warning("[sysmon] cannot register routes: %s", exc)
            return False

        instance = getattr(PromptServer, "instance", None)
        if instance is None:
            logger.warning("[sysmon] PromptServer.instance missing; routes not registered")
            return False
        routes = instance.routes
        api = self

        def json_response(payload: Any, status: int = 200):
            return web.json_response(payload, status=status, dumps=_dumps)

        async def safe(handler, request):
            """Never let a plugin bug surface as a 500 with a bare stack trace."""
            try:
                return await handler(request)
            except Exception as exc:  # pragma: no cover - defensive
                logger.exception("[sysmon] route %s failed", request.path)
                return json_response({"ok": False, "error": f"{type(exc).__name__}: {exc}"}, 500)

        # --- state ------------------------------------------------------
        @routes.get(ROUTE_PREFIX + "/state")
        async def state(request):
            async def handler(_request):
                live = api.store.live()
                live["ok"] = True
                live["config"] = api.config.public()
                live["queue"] = _queue_info()
                live["version"] = _version()
                live["server_time"] = time.time()
                return json_response(live)

            return await safe(handler, request)

        @routes.get(ROUTE_PREFIX + "/series")
        async def series(request):
            async def handler(_request):
                try:
                    limit = int(request.query.get("limit", "240"))
                except ValueError:
                    limit = 240
                limit = max(10, min(limit, 2000))
                return json_response(
                    {"ok": True, "samples": api.sampler.series(limit), "sampler": api.sampler.stats()}
                )

            return await safe(handler, request)

        # --- runs -------------------------------------------------------
        @routes.get(ROUTE_PREFIX + "/runs")
        async def runs(request):
            async def handler(_request):
                try:
                    limit = int(request.query.get("limit", "10"))
                except ValueError:
                    limit = 10
                limit = max(1, min(limit, 100))
                return json_response({"ok": True, "runs": api.store.history(limit)})

            return await safe(handler, request)

        @routes.get(ROUTE_PREFIX + "/run/{prompt_id}")
        async def run_detail(request):
            async def handler(_request):
                prompt_id = request.match_info.get("prompt_id", "")
                record = api.store.get(prompt_id)
                if record is None:
                    return json_response({"ok": False, "error": "run not found"}, 404)
                include_timeline = _truthy(request.query.get("timeline"))
                return json_response(
                    {"ok": True, "run": record.to_dict(include_timeline=include_timeline)}
                )

            return await safe(handler, request)

        @routes.delete(ROUTE_PREFIX + "/runs")
        async def clear_runs(request):
            async def handler(_request):
                api.store.clear()
                return json_response({"ok": True})

            return await safe(handler, request)

        # --- errors -----------------------------------------------------
        @routes.get(ROUTE_PREFIX + "/errors")
        async def errors(request):
            async def handler(_request):
                try:
                    limit = int(request.query.get("limit", "20"))
                except ValueError:
                    limit = 20
                return json_response({"ok": True, "errors": api.store.errors(max(1, min(limit, 200)))})

            return await safe(handler, request)

        # --- config -----------------------------------------------------
        @routes.get(ROUTE_PREFIX + "/config")
        async def get_config(request):
            async def handler(_request):
                return json_response({"ok": True, "config": api.config.public()})

            return await safe(handler, request)

        @routes.post(ROUTE_PREFIX + "/config")
        async def post_config(request):
            async def handler(_request):
                try:
                    patch = await request.json()
                except Exception:
                    return json_response({"ok": False, "error": "invalid JSON body"}, 400)
                if not isinstance(patch, dict):
                    return json_response({"ok": False, "error": "body must be an object"}, 400)
                # Never let the masked placeholder overwrite a stored key.
                if patch.get("llm_api_key") in ("***", "", None) and "llm_api_key" in patch:
                    patch.pop("llm_api_key")
                if "history_size" in patch:
                    try:
                        patch["history_size"] = max(60, min(int(patch["history_size"]), 20000))
                    except (TypeError, ValueError):
                        patch.pop("history_size", None)
                if "sample_interval_ms" in patch:
                    try:
                        patch["sample_interval_ms"] = max(200, min(int(patch["sample_interval_ms"]), 60000))
                    except (TypeError, ValueError):
                        patch.pop("sample_interval_ms", None)
                api.config.update(patch)
                api.sampler.apply_config()
                return json_response({"ok": True, "config": api.config.public()})

            return await safe(handler, request)

        # --- AI analysis ------------------------------------------------
        def _llm_disabled_response(language: str):
            message = (
                "AI 分析已被关闭。请在「设置」标签页勾选「启用 AI 分析」后重试。"
                if language != "en"
                else 'AI analysis is disabled. Enable "AI analysis" in the settings tab.'
            )
            return json_response({"ok": False, "error": message}, 403)

        @routes.post(ROUTE_PREFIX + "/analyze")
        async def analyze(request):
            async def handler(_request):
                from . import llm as llm_module

                # Consent gate: the settings toggle is labelled "enable AI
                # analysis", so it must actually stop data leaving the machine.
                if not api.config.get("llm_enabled", False):
                    return _llm_disabled_response(api.config.resolved_llm().get("language", "zh"))

                try:
                    body = await request.json()
                except Exception:
                    body = {}
                body = body if isinstance(body, dict) else {}
                prompt_id = str(body.get("prompt_id") or "")
                error_index = body.get("error_index")

                record = api.store.get(prompt_id) if prompt_id else api.store.latest_finished()
                if record is None:
                    return json_response(
                        {"ok": False, "error": "还没有已完成的运行记录可供分析。"}, 400
                    )
                run = record.to_dict(include_timeline=False)

                error_payload = None
                if body.get("error"):
                    error_payload = body["error"]
                elif record.errors:
                    try:
                        index = int(error_index) if error_index is not None else 0
                    except (TypeError, ValueError):
                        index = 0
                    index = max(0, min(index, len(record.errors) - 1))
                    error_payload = record.errors[index].to_dict()

                settings = api.config.resolved_llm()
                language = settings.get("language") or "zh"
                context = llm_module.build_context(
                    run,
                    error_payload,
                    language=language,
                    include_graph=body.get("include_graph") is not False,
                )

                history = None
                if body.get("followup"):
                    history = api._thread(record.prompt_id)

                try:
                    result = await asyncio.to_thread(
                        llm_module.analyze,
                        settings,
                        context,
                        str(body.get("question") or ""),
                        history,
                    )
                except llm_module.LLMError as exc:
                    return json_response({"ok": False, "error": str(exc)}, 400)

                # Keep a short thread so follow-up questions stay in context.
                thread = api._thread(record.prompt_id, create=True)
                thread.append(
                    {
                        "role": "user",
                        "content": json.dumps(context, ensure_ascii=False, default=str)[:6000],
                    }
                )
                thread.append({"role": "assistant", "content": result["text"][:6000]})
                del thread[:-6]

                analysis = {
                    "text": result["text"],
                    "model": result.get("model"),
                    "usage": result.get("usage"),
                    "provider": result.get("provider"),
                    "at": time.time(),
                    "context": context,
                    "had_error": bool(error_payload),
                    "question": body.get("question") or "",
                }
                if not body.get("followup"):
                    api.store.attach_analysis(record.prompt_id, analysis)

                return json_response({"ok": True, "analysis": analysis})

            return await safe(handler, request)

        @routes.post(ROUTE_PREFIX + "/test_llm")
        async def test_llm(request):
            async def handler(_request):
                from . import llm as llm_module

                settings = api.config.resolved_llm()
                try:
                    result = await asyncio.to_thread(llm_module.test_connection, settings)
                except llm_module.LLMError as exc:
                    return json_response({"ok": False, "error": str(exc)}, 400)
                return json_response(result)

            return await safe(handler, request)

        # --- preview of exactly what would be sent ----------------------
        @routes.get(ROUTE_PREFIX + "/context")
        async def context_preview(request):
            async def handler(_request):
                from . import llm as llm_module

                prompt_id = request.query.get("prompt_id", "")
                record = api.store.get(prompt_id) if prompt_id else api.store.latest_finished()
                if record is None:
                    return json_response({"ok": False, "error": "no run available"}, 404)
                error_payload = record.errors[0].to_dict() if record.errors else None
                context = llm_module.build_context(
                    record.to_dict(include_timeline=False),
                    error_payload,
                    language=api.config.resolved_llm().get("language", "zh"),
                )
                return json_response({"ok": True, "context": context})

            return await safe(handler, request)

        self._routes_registered = True
        logger.info("[sysmon] routes registered under %s", ROUTE_PREFIX)
        return True


def _dumps(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False, default=str)


def _truthy(value: Optional[str]) -> bool:
    return str(value or "").lower() in ("1", "true", "yes", "on")


def _queue_info() -> Dict[str, Any]:
    """Pending/running counts, if the running ComfyUI exposes them."""
    try:
        from server import PromptServer  # type: ignore

        instance = getattr(PromptServer, "instance", None)
        if instance is None:
            return {}
        queue = getattr(instance, "prompt_queue", None)
        if queue is None:
            return {}
        info: Dict[str, Any] = {}
        try:
            info["running"] = len(queue.get_current_queue()[0])
            info["pending"] = len(queue.get_current_queue()[1])
        except Exception:
            pass
        return info
    except Exception:
        return {}


def _version() -> str:
    try:
        from . import __version__

        return __version__
    except Exception:
        return "unknown"
