"""
core/ai_scraper.py

Abre el navegador, envía prompts en secuencia y devuelve respuestas.
"""
import asyncio, re
from core.browser import BrowserSession, AI_SITES, check_playwright, install_playwright, C
from core.web_log  import log_prompt, log_response, log_error

SITE_PRIORITY = ["claude", "chatgpt", "gemini", "qwen"]


def parse_steps(response: str) -> list[dict]:
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


async def _run_multiturn(prompts: list[str], site_key: str, objetivo: str) -> list[str]:
    """
    Una sesión del navegador, todos los prompts en secuencia.
    NO imprime mientras espera — imprime solo el resultado final.
    """
    site_name = AI_SITES[site_key]["name"]
    responses = []

    async with BrowserSession(site_key) as session:
        if await session.needs_login():
            await session.wait_for_login()
            await session._page.goto(
                AI_SITES[site_key]["url"],
                wait_until="domcontentloaded", timeout=30000
            )

        for idx, prompt in enumerate(prompts, 1):
            print(f"  [Turno {idx}/{len(prompts)}] Enviando...", flush=True)
            log_prompt(site_name, objetivo, prompt)

            resp = await session.send_prompt(prompt)   # espera respuesta completa

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
    return asyncio.run(_multiturn_async(prompts, preferred_site, objetivo))


# Compatibilidad
def ask_ai_web_multiturn(prompts, preferred_site=None, objetivo=""):
    return ask_ai_multiturn(prompts, preferred_site, objetivo)

def ask_ai_web_sync(objetivo, preferred_site=None, raw_prompt=None):
    prompt = raw_prompt if raw_prompt else objetivo
    _, responses = ask_ai_multiturn([prompt], preferred_site, objetivo)
    resp = responses[0] if responses else ""
    return resp, parse_steps(resp)