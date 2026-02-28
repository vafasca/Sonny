"""Capa de comunicación LLM vía navegador para Sonny.

Este módulo NO ejecuta acciones ni parsea pasos.
Solo envía prompts al proveedor web y devuelve texto crudo.
"""

from __future__ import annotations

import asyncio
import threading
from typing import Optional

from core.browser import AI_SITES, BrowserSession, C, check_playwright, install_playwright
from core.web_log import log_error, log_prompt, log_response

SITE_PRIORITY = ["claude", "chatgpt", "gemini", "qwen"]
_PERSISTENT_SESSIONS: dict[str, BrowserSession] = {}

_RUNTIME_LOOP: asyncio.AbstractEventLoop | None = None
_RUNTIME_THREAD: threading.Thread | None = None
_RUNTIME_LOCK = threading.Lock()


def _ensure_runtime_loop() -> asyncio.AbstractEventLoop:
    global _RUNTIME_LOOP, _RUNTIME_THREAD
    with _RUNTIME_LOCK:
        if _RUNTIME_LOOP and _RUNTIME_LOOP.is_running():
            return _RUNTIME_LOOP

        _RUNTIME_LOOP = asyncio.new_event_loop()

        def _runner(loop: asyncio.AbstractEventLoop):
            asyncio.set_event_loop(loop)
            loop.run_forever()

        _RUNTIME_THREAD = threading.Thread(target=_runner, args=(_RUNTIME_LOOP,), daemon=True)
        _RUNTIME_THREAD.start()
        return _RUNTIME_LOOP


def shutdown_ai_scraper_runtime() -> None:
    """Cierra runtime y sesiones persistentes de navegador."""
    global _RUNTIME_LOOP, _RUNTIME_THREAD

    async def _close_sessions():
        for key, session in list(_PERSISTENT_SESSIONS.items()):
            try:
                await session.close()
            except Exception:
                pass
            finally:
                _PERSISTENT_SESSIONS.pop(key, None)

    loop = _RUNTIME_LOOP
    if loop and loop.is_running():
        future = asyncio.run_coroutine_threadsafe(_close_sessions(), loop)
        try:
            future.result(timeout=8)
        except Exception:
            pass
        loop.call_soon_threadsafe(loop.stop)

    _RUNTIME_LOOP = None
    _RUNTIME_THREAD = None


async def _call_llm_async(prompt: str, preferred_site: Optional[str] = None, objetivo: str = "") -> str:
    if not check_playwright():
        install_playwright()

    sites = [preferred_site] if preferred_site in AI_SITES else SITE_PRIORITY

    for site_key in sites:
        site_name = AI_SITES[site_key]["name"]
        try:
            session = _PERSISTENT_SESSIONS.get(site_key)
            if session is None:
                session = BrowserSession(site_key)
                _PERSISTENT_SESSIONS[site_key] = session
                await session.start()

            log_prompt(site_name, prompt, objetivo or prompt)
            raw_response = await session.send_prompt(prompt)
            response = (raw_response or "").strip()

            if response:
                log_response(site_name, response, 0)
                return response

            log_error(site_name, "Respuesta vacía del LLM")
        except Exception as exc:
            log_error(site_name, str(exc))
            print(f"{C.YELLOW}⚠️ Falló {site_name}, intentando siguiente proveedor...{C.RESET}")
            try:
                broken = _PERSISTENT_SESSIONS.pop(site_key, None)
                if broken:
                    await broken.close()
            except Exception:
                pass

    raise RuntimeError("Ninguna IA web devolvió una respuesta utilizable.")


def call_llm(prompt: str) -> str:
    """Envía un prompt y devuelve texto crudo del LLM."""
    loop = _ensure_runtime_loop()
    future = asyncio.run_coroutine_threadsafe(_call_llm_async(prompt, objetivo=prompt), loop)
    return future.result()


# Compatibilidad retroactiva

def ask_ai_multiturn(prompts: list[str], preferred_site: str = None, objetivo: str = "") -> tuple[str, list[str]]:
    responses: list[str] = []
    site_used = preferred_site or SITE_PRIORITY[0]
    for prompt in prompts:
        loop = _ensure_runtime_loop()
        future = asyncio.run_coroutine_threadsafe(
            _call_llm_async(prompt, preferred_site=preferred_site, objetivo=objetivo or prompt), loop
        )
        responses.append(future.result())
    return site_used, responses


def ask_ai_web_multiturn(prompts, preferred_site=None, objetivo=""):
    return ask_ai_multiturn(prompts, preferred_site, objetivo)


def ask_ai_web_sync(objetivo, preferred_site=None, raw_prompt=None):
    prompt = raw_prompt if raw_prompt else objetivo
    response = call_llm(prompt)
    return response, []
