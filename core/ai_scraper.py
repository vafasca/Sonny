"""
core/ai_scraper.py

Abre el navegador, envía prompts en secuencia y devuelve respuestas.
v2: parse_steps soporta respuestas JSON además del formato texto original.
"""
import atexit
import asyncio
import json
import re
import threading

from core.browser import BrowserSession, AI_SITES, check_playwright, install_playwright, C
from core.web_log  import log_prompt, log_response, log_error

SITE_PRIORITY = ["claude", "chatgpt", "gemini", "qwen"]
_PERSISTENT_SESSIONS: dict[str, BrowserSession] = {}

# Runtime async persistente en hilo aparte para no romper objetos Playwright
# entre múltiples llamadas síncronas a ask_ai_multiturn().
_RUNTIME_LOOP: asyncio.AbstractEventLoop | None = None
_RUNTIME_THREAD: threading.Thread | None = None
_RUNTIME_LOCK = threading.Lock()


def parse_steps(response: str) -> list[dict]:
    """
    Extrae pasos de la respuesta de la IA.
    Soporta dos formatos:
      1. JSON (formato preferido): {"steps": [{"description":..., "cmd":..., "files":[...]}]}
      2. Texto con comandos (fallback): líneas que empiezan con npm/ng/pip/etc.
    """
    # Intento 1: JSON
    json_steps = _parse_steps_json(response)
    if json_steps is not None:
        return json_steps

    # Fallback: parseo de texto con comandos
    return _parse_steps_text(response)


def _parse_steps_json(response: str) -> list[dict] | None:
    """Intenta extraer steps de una respuesta JSON."""
    clean = response.strip()
    # Quitar backticks de markdown
    clean = re.sub(r'^```(?:json)?\s*', '', clean, flags=re.MULTILINE)
    clean = re.sub(r'```\s*$', '', clean, flags=re.MULTILINE)
    clean = clean.strip()

    # Intentar parsear directamente
    data = None
    try:
        data = json.loads(clean)
    except json.JSONDecodeError:
        # Buscar JSON embebido en texto
        match = re.search(r'\{.*\}', clean, re.DOTALL)
        if match:
            try:
                data = json.loads(match.group())
            except json.JSONDecodeError:
                pass

    if not data:
        return None

    steps_raw = data.get("steps", [])
    if not steps_raw:
        # Si el JSON tiene cmd/files directamente (sin wrapper "steps")
        if "cmd" in data or "files" in data:
            steps_raw = [data]
        else:
            return None

    result = []
    SKIP_CMDS = ("ng serve", "npm start", "npm run dev", "cd ", "node -v", "npm -v")

    for s in steps_raw:
        if not isinstance(s, dict):
            continue
        cmd = s.get("cmd") or None
        if cmd and isinstance(cmd, str):
            cmd = cmd.strip()
            if not cmd or cmd.upper() in ("NINGUNO", "NONE", "N/A", "NULL"):
                cmd = None
        # Filtrar comandos de desarrollo/arranque
        if cmd and any(cmd.lower().startswith(sk) for sk in SKIP_CMDS):
            cmd = None

        # Convertir a formato "cmd" simple para compatibilidad
        if cmd:
            result.append({"type": "cmd", "value": cmd})

    return result if result else None


def _parse_steps_text(response: str) -> list[dict]:
    """Parser de texto original — extrae comandos de líneas de texto."""
    steps, seen = [], set()
    CMD_STARTS = ("npm ","npx ","ng ","pip ","python ","node ","mkdir ","git ")
    SKIP = ("ng serve","npm start","npm run ","cd ","node -v","npm -v")
    for line in response.splitlines():
        line = re.sub(r'^[\d]+[.):\-\s]+','',line.strip()).lstrip('`$>').strip()
        if len(line) < 5: continue
        if any(line.lower().startswith(s) for s in SKIP): continue
        if any(line.startswith(s) for s in CMD_STARTS) and line not in seen:
            seen.add(line)
            steps.append({"type":"cmd","value":line})
    return steps


def _runtime_loop_worker(loop: asyncio.AbstractEventLoop):
    asyncio.set_event_loop(loop)
    loop.run_forever()


def _ensure_runtime_loop() -> asyncio.AbstractEventLoop:
    global _RUNTIME_LOOP, _RUNTIME_THREAD
    with _RUNTIME_LOCK:
        if _RUNTIME_LOOP and _RUNTIME_THREAD and _RUNTIME_THREAD.is_alive():
            return _RUNTIME_LOOP

        loop = asyncio.new_event_loop()
        thread = threading.Thread(
            target=_runtime_loop_worker,
            args=(loop,),
            name="sonny-ai-scraper-loop",
            daemon=True,
        )
        thread.start()
        _RUNTIME_LOOP = loop
        _RUNTIME_THREAD = thread
        return loop


async def _close_all_sessions_async():
    for key, session in list(_PERSISTENT_SESSIONS.items()):
        try:
            await session.close()
        except Exception as e:
            log_error(AI_SITES.get(key, {}).get("name", key), f"close_session: {e}")
    _PERSISTENT_SESSIONS.clear()


def _shutdown_runtime_loop():
    global _RUNTIME_LOOP, _RUNTIME_THREAD
    with _RUNTIME_LOCK:
        loop = _RUNTIME_LOOP
        thread = _RUNTIME_THREAD
        _RUNTIME_LOOP = None
        _RUNTIME_THREAD = None

    if not loop or not thread:
        return

    try:
        future = asyncio.run_coroutine_threadsafe(_close_all_sessions_async(), loop)
        future.result(timeout=5)
    except Exception:
        pass

    try:
        loop.call_soon_threadsafe(loop.stop)
    except Exception:
        pass

    thread.join(timeout=2)


atexit.register(_shutdown_runtime_loop)


async def _run_multiturn(prompts: list[str], site_key: str, objetivo: str) -> list[str]:
    """
    Una sesión del navegador, todos los prompts en secuencia.
    """
    site_name = AI_SITES[site_key]["name"]
    responses = []

    session = _PERSISTENT_SESSIONS.get(site_key)
    if session is None:
        session = BrowserSession(site_key)
        _PERSISTENT_SESSIONS[site_key] = session

    try:
        await session.start()
    except Exception:
        # Si la sesión quedó en mal estado, recrearla
        session = BrowserSession(site_key)
        _PERSISTENT_SESSIONS[site_key] = session
        await session.start()

    if await session.needs_login():
        await session.wait_for_login()
        await session._page.goto(
            AI_SITES[site_key]["url"],
            wait_until="domcontentloaded", timeout=30000
        )

    for idx, prompt in enumerate(prompts, 1):
        print(f"  [Turno {idx}/{len(prompts)}] Enviando...", flush=True)
        log_prompt(site_name, objetivo, prompt)

        try:
            resp = await session.send_prompt(prompt)
        except Exception:
            # Si el usuario cerró manualmente la ventana, relanzar una sola vez
            fresh = BrowserSession(site_key)
            _PERSISTENT_SESSIONS[site_key] = fresh
            await fresh.start()
            session = fresh
            resp = await session.send_prompt(prompt)

        if not resp or len(resp) < 20:
            log_error(site_name, f"Turno {idx}: respuesta vacía")
            print(f"  ⚠️  Turno {idx}: sin respuesta", flush=True)
            responses.append("")
        else:
            log_response(site_name, resp, len(parse_steps(resp)))
            print(f"  ✅ Turno {idx}: {len(resp)} chars", flush=True)
            responses.append(resp)

    return responses


async def _multiturn_async(prompts: list[str], preferred_site: str = None,
                           objetivo: str = "") -> tuple[str, list[str]]:
    if not check_playwright():
        install_playwright()

    sites = (
        [preferred_site] + [s for s in SITE_PRIORITY if s != preferred_site]
        if preferred_site and preferred_site in AI_SITES
        else list(SITE_PRIORITY)
    )

    for site_key in sites:
        site_name = AI_SITES[site_key]["name"]
        print(f"\n  {C.CYAN}🌐 Conectando con {site_name}...{C.RESET}", flush=True)
        try:
            responses = await _run_multiturn(prompts, site_key, objetivo)
            if any(r for r in responses):
                return site_key, responses
        except Exception as e:
            log_error(AI_SITES[site_key]["name"], str(e))
            print(f"  {C.RED}❌ {site_name} falló: {e}{C.RESET}", flush=True)

    raise RuntimeError("Ninguna IA web estuvo disponible.")


def ask_ai_multiturn(prompts: list[str], preferred_site: str = None,
                     objetivo: str = "") -> tuple[str, list[str]]:
    loop = _ensure_runtime_loop()
    future = asyncio.run_coroutine_threadsafe(
        _multiturn_async(prompts, preferred_site, objetivo), loop
    )
    return future.result()


# Compatibilidad
def ask_ai_web_multiturn(prompts, preferred_site=None, objetivo=""):
    return ask_ai_multiturn(prompts, preferred_site, objetivo)


def ask_ai_web_sync(objetivo, preferred_site=None, raw_prompt=None):
    prompt = raw_prompt if raw_prompt else objetivo
    _, responses = ask_ai_multiturn([prompt], preferred_site, objetivo)
    resp = responses[0] if responses else ""
    return resp, parse_steps(resp)
