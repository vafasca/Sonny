"""
browser.py — v9  (Extracción por-IA totalmente separada)
══════════════════════════════════════════════════════════════════════════════
HISTORIAL DE FIXES
──────────────────────────────────────────────────────────────────────────────
FIX 1 — PASTE LIMPIO:
  PROBLEMA: Al pegar texto largo en el textarea, a veces se duplicaba o
  el cursor quedaba mal posicionado.
  SOLUCIÓN: Un solo intento de paste vía pyperclip + clipboard.

FIX 2 — RESPUESTA NUEVA (render-count, SOLO para Claude):
  PROBLEMA: multi-turno devolvía siempre la misma respuesta (la anterior).
  SOLUCIÓN: contar bloques antes de enviar, esperar uno nuevo.

FIX 3 — TIMEOUT EXTENDIDO + ESPERA EXTRA (SOLO para Claude):
  PROBLEMA: Claude con razonamiento extendido puede tardar 3-5 min.
  SOLUCIÓN: max_wait=360s + loop extra de hasta 120s si sigue generando.

FIX 4 — EXTRACCIÓN POR-IA TOTALMENTE AISLADA (v9):
  PROBLEMA: _COUNT_RENDERS_JS y _EXTRACT_NEW_RESPONSE_JS usaban
  [data-test-render-count], un selector exclusivo de Claude. ChatGPT
  nunca lo tiene → se quedaba esperando infinitamente.
  SOLUCIÓN: Cada entrada de AI_SITES ahora incluye sus propios campos:
    · "count_js"   → JS que devuelve el nº de respuestas actuales (int)
    · "extract_js" → JS(prevCount) que devuelve el texto de la respuesta nueva
  Así añadir/cambiar una IA nunca afecta a las demás.
"""
import asyncio, os, sys, time, re
import pyperclip
from pathlib import Path

class C:
    CYAN="\033[96m"; GREEN="\033[92m"; YELLOW="\033[93m"
    RED="\033[91m";  BOLD="\033[1m";   DIM="\033[2m"; RESET="\033[0m"

SESSIONS_DIR      = Path(__file__).parent.parent / "sessions"
SESSIONS_DIR.mkdir(exist_ok=True)
EDGE_PROFILES_DIR = Path(__file__).parent.parent / "perfil_edge"
EDGE_PROFILES_DIR.mkdir(exist_ok=True)

USE_SYSTEM_CHROME = (os.environ.get("SONNY_USE_SYSTEM_CHROME") or "").strip().lower() in {"1","true","yes","si","sí"}
CHROME_CDP_URL    = (os.environ.get("SONNY_CHROME_CDP_URL") or "http://127.0.0.1:9222").strip()

# ══════════════════════════════════════════════════════════════════════════════
#  CONFIGURACIÓN DE SITIOS — CADA IA TIENE SU PROPIA LÓGICA DE EXTRACCIÓN
#
#  count_js   → función JS que devuelve (int) cuántas respuestas hay ahora.
#               Se llama ANTES de enviar el prompt para guardar la "línea base".
#
#  extract_js → función JS que recibe (prevCount: int) y devuelve (string)
#               el texto de la respuesta nueva, o '' si todavía no hay.
#
#  Para agregar una nueva IA: solo rellena estos dos campos.
#  Las IAs existentes NUNCA se tocan al agregar una nueva.
# ══════════════════════════════════════════════════════════════════════════════

AI_SITES = {
    # ── Claude ────────────────────────────────────────────────────────────────
    "claude": {
        "url":          "https://claude.ai/new",
        "name":         "Claude.ai",
        "input_sel":    '[contenteditable="true"]',
        "send_sel":     'button[aria-label="Send message"], button[aria-label="Enviar mensaje"], button[aria-label="Enviar"]',
        "response_sel": '[data-testid="assistant-message"], .font-claude-message, [class*="font-claude-message"]',
        "done_sel":     'button[aria-label="Send message"]:not([disabled]), button[aria-label="Enviar mensaje"]:not([disabled]), button[aria-label="Enviar"]:not([disabled])',
        "session_file": "claude_session",
        # ── JS específico de Claude ──────────────────────────────────────────
        "count_js": r"""
            () => {
                return document.querySelectorAll('[data-test-render-count]').length;
            }
        """,
        "extract_js": r"""
            (prevCount) => {
                const renders = Array.from(
                    document.querySelectorAll('[data-test-render-count]')
                );
                if (renders.length <= prevCount) return '';

                const newest = renders[renders.length - 1];
                const responseDiv = newest.querySelector('.font-claude-response') || newest;
                const clone = responseDiv.cloneNode(true);

                // Eliminar bloques de pensamiento interno
                clone.querySelectorAll(
                    '[data-testid="thinking-block"], .thinking-block, details'
                ).forEach(t => t.remove());

                return clone.innerText.trim();
            }
        """,
    },

    # ── ChatGPT ───────────────────────────────────────────────────────────────
    "chatgpt": {
        "url":          "https://chatgpt.com/",
        "name":         "ChatGPT",
        "input_sel":    '#prompt-textarea',
        "send_sel":     'button[data-testid="send-button"]',
        "response_sel": '[data-message-author-role="assistant"]',
        "done_sel":     'button[data-testid="send-button"]:not([disabled])',
        "session_file": "chatgpt_session",
        # ── JS específico de ChatGPT ─────────────────────────────────────────
        "count_js": r"""
            () => {
                return document.querySelectorAll(
                    '[data-message-author-role="assistant"]'
                ).length;
            }
        """,
        "extract_js": r"""
            (prevCount) => {
                const msgs = Array.from(
                    document.querySelectorAll('[data-message-author-role="assistant"]')
                );
                if (msgs.length <= prevCount) return '';

                const newest = msgs[msgs.length - 1];
                const clone = newest.cloneNode(true);

                // Quitar botones de acción (copiar, pulgar, etc.)
                clone.querySelectorAll(
                    'button, [data-testid="copy-turn-action-button"], ' +
                    '[class*="action"], [class*="feedback"]'
                ).forEach(el => el.remove());

                // ── FIX NEWLINES en código ────────────────────────────────
                // Un nodo clonado/desconectado NO tiene CSS aplicado, así que
                // innerText ignora white-space:pre y colapsa todo en una línea.
                // Solución: reemplazar cada <pre> con un nodo de texto que
                // contenga el textContent literal (que SÍ conserva los \n).
                clone.querySelectorAll('pre').forEach(pre => {
                    const raw = '\n' + pre.textContent + '\n';
                    pre.parentNode.replaceChild(
                        document.createTextNode(raw), pre
                    );
                });

                // Convertir <br> a \n explícito antes de leer innerText
                clone.querySelectorAll('br').forEach(br => {
                    br.parentNode.replaceChild(
                        document.createTextNode('\n'), br
                    );
                });

                return clone.innerText.trim();
            }
        """,
    },

    # ── Gemini ────────────────────────────────────────────────────────────────
    "gemini": {
        "url":          "https://gemini.google.com/app",
        "name":         "Gemini",
        "input_sel":    ".ql-editor",
        "send_sel":     'button[aria-label="Send message"]',
        "response_sel": ".model-response-text",
        "done_sel":     'button[aria-label="Send message"]:not([disabled])',
        "session_file": "gemini_session",
        # ── JS específico de Gemini ──────────────────────────────────────────
        "count_js": r"""
            () => {
                return document.querySelectorAll('.model-response-text').length;
            }
        """,
        "extract_js": r"""
            (prevCount) => {
                const msgs = Array.from(
                    document.querySelectorAll('.model-response-text')
                );
                if (msgs.length <= prevCount) return '';
                return msgs[msgs.length - 1].innerText.trim();
            }
        """,
    },

    # ── Qwen ──────────────────────────────────────────────────────────────────
    "qwen": {
        "url":          "https://chat.qwen.ai/",
        "name":         "Qwen",
        "input_sel":    'textarea',
        "send_sel":     'button[type="submit"]',
        "response_sel": ".markdown-body",
        "done_sel":     'button[type="submit"]:not([disabled])',
        "session_file": "qwen_session",
        # ── JS específico de Qwen ────────────────────────────────────────────
        "count_js": r"""
            () => {
                return document.querySelectorAll('.markdown-body').length;
            }
        """,
        "extract_js": r"""
            (prevCount) => {
                const msgs = Array.from(
                    document.querySelectorAll('.markdown-body')
                );
                if (msgs.length <= prevCount) return '';
                return msgs[msgs.length - 1].innerText.trim();
            }
        """,
    },
}

# ── Helpers de Playwright ──────────────────────────────────────────────────────

def check_playwright() -> bool:
    try:
        import playwright; return True
    except ImportError:
        return False

def install_playwright():
    import subprocess
    print(f"  {C.YELLOW}Instalando Playwright...{C.RESET}")
    subprocess.run([sys.executable, "-m", "pip", "install", "playwright",
                    "--break-system-packages", "-q"], check=True)
    subprocess.run([sys.executable, "-m", "playwright", "install", "chromium"], check=True)
    print(f"  {C.GREEN}✅ Playwright instalado{C.RESET}")


async def _query_first(page, selector_str: str):
    for sel in selector_str.split(","):
        sel = sel.strip()
        if not sel: continue
        try:
            el = await page.query_selector(sel)
            if el: return el
        except Exception:
            continue
    return None


# JS para leer artefactos en iframes (Claude descarga archivos en iframes)
_EXTRACT_ARTIFACT_JS = r"""
() => {
  const results = [];
  const tryFrame = (win, depth) => {
    if (depth > 3) return;
    try {
      const body = win.document.body;
      if (!body) return;
      const text = (body.innerText || body.textContent || '').trim();
      if (text && text.length > 50) results.push(text);
    } catch(e) {}
    try {
      for (const frame of win.frames) tryFrame(frame, depth + 1);
    } catch(e) {}
  };
  for (const iframe of document.querySelectorAll('iframe')) {
    try { if (iframe.contentWindow) tryFrame(iframe.contentWindow, 0); } catch(e) {}
  }
  return results.join('\n\n---ARTIFACT---\n\n');
}
"""


# ══════════════════════════════════════════════════════════════════════════════
#  BROWSER SESSION
# ══════════════════════════════════════════════════════════════════════════════

class BrowserSession:
    """Sesión de navegador persistente para una IA específica."""

    def __init__(self, site_key: str):
        if site_key not in AI_SITES:
            raise ValueError(f"Sitio desconocido: {site_key}. "
                             f"Opciones: {list(AI_SITES.keys())}")
        self.site_key             = site_key
        self.site                 = AI_SITES[site_key]
        self._browser             = None
        self._context             = None
        self._page                = None
        self._pw                  = None
        self._started             = False          # ← guard anti-relanzamiento
        self._using_system_chrome = False
        self._profile_dir = EDGE_PROFILES_DIR / f"profile_{site_key}"
        self._profile_dir.mkdir(exist_ok=True)

    # ── Ciclo de vida ──────────────────────────────────────────────────────────

    async def _start_with_system_chrome(self) -> bool:
        """Intenta conectar a Chrome/Edge vía CDP. Devuelve True si lo logra."""
        if not USE_SYSTEM_CHROME:
            return False
        try:
            self._browser = await self._pw.chromium.connect_over_cdp(CHROME_CDP_URL)
            self._using_system_chrome = True
            contexts      = self._browser.contexts
            self._context = contexts[0] if contexts else await self._browser.new_context()
            pages         = self._context.pages
            self._page    = pages[0] if pages else await self._context.new_page()
            print(f"  {C.GREEN}✅ Conectado a Chrome real: {CHROME_CDP_URL}{C.RESET}")
            return True
        except Exception as e:
            print(f"  {C.YELLOW}⚠️  CDP falló: {e} — usando Edge persistente{C.RESET}")
            return False

    async def start(self):
        # ── Guard: si ya está vivo, no relanzar ─────────────────────────────
        if self._started:
            return self

        if not check_playwright():
            install_playwright()

        from playwright.async_api import async_playwright
        self._pw = await async_playwright().start()

        if not await self._start_with_system_chrome():
            self._context = await self._pw.chromium.launch_persistent_context(
                str(self._profile_dir),
                channel="msedge",                  # ← siempre Edge, todas las IAs
                headless=False,
                ignore_default_args=["--enable-automation"],
                args=[
                    "--start-maximized",
                    "--disable-blink-features=AutomationControlled",
                    "--disable-infobars",
                ],
                viewport={"width": 1280, "height": 800},
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/120.0.0.0 Safari/537.36"
                ),
            )
            pages      = self._context.pages
            self._page = pages[0] if pages else await self._context.new_page()
            await self._context.add_init_script(
                "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
            )

        await self._context.grant_permissions(["clipboard-read", "clipboard-write"])
        self._started = True
        return self

    async def close(self):
        if not self._started:
            return
        if not self._using_system_chrome and self._context:
            try:
                await self._context.close()
            except Exception:
                pass
        if self._browser:
            try:
                await self._browser.close()
            except Exception:
                pass
        if self._pw:
            try:
                await self._pw.stop()
            except Exception:
                pass
        self._browser = self._context = self._page = self._pw = None
        self._started = self._using_system_chrome = False

    async def __aenter__(self):
        return await self.start()
    async def __aexit__(self, *a):
        await self.close()

    # ── ChatGPT login helpers ──────────────────────────────────────────────────

    def _chatgpt_env_credentials_set(self) -> bool:
        if self.site_key != "chatgpt": return False
        return bool((os.environ.get("CHATGPT_EMAIL") or "").strip() and
                    (os.environ.get("CHATGPT_PASSWORD") or "").strip())

    def _chatgpt_allow_automated_login(self) -> bool:
        return (os.environ.get("CHATGPT_AUTOMATED_LOGIN") or "").strip().lower() in {"1","true","yes","si","sí"}

    async def _chatgpt_has_login_button(self) -> bool:
        if self.site_key != "chatgpt": return False
        for sel in ['button:has-text("Iniciar sesión")', 'a:has-text("Iniciar sesión")',
                    'button:has-text("Log in")',          'a:has-text("Log in")']:
            try:
                if await self._page.query_selector(sel): return True
            except Exception: pass
        return False

    async def _wait_until_chatgpt_ready_after_login(self, timeout_s=120) -> bool:
        if self.site_key != "chatgpt": return True
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            try:
                url = (self._page.url or "").lower()
                if "accounts.google.com" in url or "auth.openai.com" in url:
                    await asyncio.sleep(1); continue
                if await self._page.query_selector(self.site["input_sel"]):
                    if not await self._chatgpt_has_login_button(): return True
                await self._page.wait_for_timeout(2000)
            except Exception:
                await asyncio.sleep(1)
        return False

    # ── Login manual ───────────────────────────────────────────────────────────

    async def needs_login(self, navigate_if_needed=True) -> bool:
        page = self._page
        site = self.site
        if navigate_if_needed:
            try:
                site_host = re.sub(r"^https?://", "", site["url"]).split("/")[0].lower()
                if site_host not in (page.url or "").lower():
                    await page.goto(site["url"], wait_until="domcontentloaded", timeout=30000)
                    await asyncio.sleep(3)
            except Exception: pass
        try:
            if await page.query_selector(site["input_sel"]):
                return False
        except Exception: pass
        return True

    async def wait_for_login(self):
        site = self.site
        print(f"\n  {C.YELLOW}🔐 Inicia sesión en {site['name']}.")
        print(f"  Loguéate y luego vuelve aquí.{C.RESET}")
        print(f"  {C.CYAN}Presiona ENTER cuando hayas iniciado sesión...{C.RESET}")
        input()
        if self.site_key == "chatgpt":
            await self._wait_until_chatgpt_ready_after_login()
        print(f"  {C.GREEN}✅ Sesión persistida en {self._profile_dir}{C.RESET}\n")

    # ══════════════════════════════════════════════════════════════════════════
    #  EXTRACCIÓN POR-IA — usa los JS del diccionario AI_SITES
    # ══════════════════════════════════════════════════════════════════════════

    async def _count_responses(self) -> int:
        """Cuenta las respuestas actuales usando el JS del sitio activo."""
        try:
            count = await self._page.evaluate(self.site["count_js"])
            return int(count or 0)
        except Exception:
            return 0

    async def _extract_new_response(self, prev_count: int) -> str:
        """
        Extrae la respuesta nueva usando el JS del sitio activo.
        Devuelve '' si todavía no aparece el bloque nuevo.
        """
        try:
            texto = await self._page.evaluate(self.site["extract_js"], prev_count)
            return (texto or "").strip()
        except Exception:
            return ""

    async def _extract_latest_response(self) -> str:
        """
        Devuelve la última respuesta visible del asistente, sin depender de prev_count.
        Útil como fallback cuando el contador del DOM no sube por virtualización.
        """
        try:
            txt = await self._page.evaluate(self.site["extract_js"], -1)
            txt = (txt or "").strip()
            if txt:
                return txt
        except Exception:
            pass
        try:
            nodes = await self._page.query_selector_all(self.site["response_sel"])
            if not nodes:
                return ""
            last = nodes[-1]
            txt = await last.inner_text()
            return (txt or "").strip()
        except Exception:
            return ""

    # ── Detección de generación activa ────────────────────────────────────────

    async def _is_generating(self) -> bool:
        page = self._page
        site = self.site
        # Si el botón de envío ya está habilitado → terminó
        for sel in site["done_sel"].split(","):
            sel = sel.strip()
            if not sel: continue
            try:
                btn = await page.query_selector(sel)
                if btn: return False
            except Exception: continue
        # Selectores genéricos de "stop/spinner"
        for sel in [
            'button[aria-label="Stop"]', 'button[aria-label="Detener"]',
            'button[aria-label="Stop generating"]', '[data-testid="stop-button"]',
            '[class*="stop-button"]', '[class*="spinner"]',
            '[class*="loading"]', '[class*="generating"]', '[class*="streaming"]',
        ]:
            try:
                if await page.query_selector(sel): return True
            except Exception: continue
        return False

    # ── Artefactos en iframes (Claude) ────────────────────────────────────────

    async def _extract_from_artifacts(self) -> str:
        page  = self._page
        parts = []
        try:
            for frame in page.frames:
                if frame == page.main_frame: continue
                try:
                    content = await frame.evaluate("""
                        () => {
                          const body = document.body;
                          if (!body) return '';
                          return body.innerText || body.textContent || '';
                        }
                    """)
                    content = (content or "").strip()
                    if content and len(content) > 30:
                        parts.append(content)
                except Exception:
                    pass
        except Exception:
            pass
        return "\n\n".join(parts) if parts else ""

    # ══════════════════════════════════════════════════════════════════════════
    #  WAIT FOR RESPONSE — v9
    #  · Usa _count_responses() y _extract_new_response() (por-IA)
    #  · Claude: max_wait=360s + espera extra 120s si sigue generando
    #  · ChatGPT/otros: max_wait=180s (suficiente para respuestas normales)
    # ══════════════════════════════════════════════════════════════════════════

    async def _wait_for_response(self, max_wait: int = 0,
                                  prev_count: int = 0,
                                  prev_latest: str = "") -> str:
        # Ajustar timeout por defecto según la IA
        if max_wait == 0:
            max_wait = 360 if self.site_key == "claude" else 180

        start         = time.time()
        last_txt      = ""
        stable        = 0
        no_text_count = 0
        new_appeared  = False
        # ChatGPT a veces responde con bloques muy cortos (ej: "ng new ...").
        # Si exigimos >40 chars, el loop se queda esperando aunque la respuesta
        # ya esté visible en pantalla.
        min_chars = 8 if self.site_key == "chatgpt" else 20

        await asyncio.sleep(3)

        while time.time() - start < max_wait:
            await asyncio.sleep(2)

            try:
                current = await self._extract_new_response(prev_count)
            except Exception:
                current = ""

            if current and len(current.strip()) >= min_chars:
                no_text_count = 0
                if not new_appeared:
                    new_appeared = True
                    print(f"  {C.DIM}  Nueva respuesta detectada ({len(current)} chars)...{C.RESET}")

                if current == last_txt:
                    stable += 1
                    if stable >= 3:
                        if not await self._is_generating():
                            # Agregar artefactos iframe si los hay (solo Claude los usa)
                            if self.site_key == "claude":
                                try:
                                    artifact_text = await self._extract_from_artifacts()
                                    if artifact_text and len(artifact_text) > 30:
                                        current = current + "\n\n" + artifact_text
                                        print(f"  {C.DIM}  📦 Artefacto iframe detectado ({len(artifact_text)} chars){C.RESET}")
                                except Exception:
                                    pass
                            print(f"  {C.DIM}  Respuesta lista ({len(current)} chars){C.RESET}")
                            return current
                        stable = 0
                else:
                    stable   = 0
                    last_txt = current
            elif current and len(current.strip()) >= 4:
                # Respuesta corta (frecuente en prompts de comando único).
                # Si ya terminó de generar, la aceptamos sin esperar largos ciclos.
                no_text_count = 0
                if not await self._is_generating():
                    print(f"  {C.DIM}  Respuesta corta detectada ({len(current)} chars){C.RESET}")
                    return current.strip()
            else:
                # Fallback para proveedores con virtualización del DOM (ChatGPT).
                # Si no aparece "nuevo bloque" por count, igual intentamos detectar
                # si cambió el último mensaje respecto al turno anterior.
                if self.site_key == "chatgpt":
                    latest = await self._extract_latest_response()
                    if latest and latest != prev_latest and len(latest.strip()) >= 4:
                        if not await self._is_generating():
                            print(f"  {C.DIM}  Respuesta detectada por fallback ({len(latest)} chars){C.RESET}")
                            return latest
                no_text_count += 1
                if no_text_count == 8:
                    print(f"  {C.YELLOW}  ⚠️  Aún esperando nueva respuesta...{C.RESET}")
                if no_text_count >= 12 and last_txt:
                    if not await self._is_generating():
                        return last_txt

        # ── Timeout alcanzado ─────────────────────────────────────────────────
        txt = last_txt
        if txt:
            # Espera extra solo para Claude (razonamiento extendido puede durar mucho)
            if self.site_key == "claude" and await self._is_generating():
                print(f"  {C.YELLOW}  ⚠️  Timeout ({max_wait}s) pero Claude sigue generando — "
                      f"esperando hasta 120s más...{C.RESET}")
                extra_waited = 0
                while extra_waited < 120:
                    await asyncio.sleep(5)
                    extra_waited += 5
                    try:
                        new_txt = await self._extract_new_response(prev_count)
                        if new_txt and len(new_txt) > len(txt):
                            txt = new_txt
                    except Exception:
                        pass
                    if not await self._is_generating():
                        await asyncio.sleep(2)
                        try:
                            final_txt = await self._extract_new_response(prev_count)
                            if final_txt and len(final_txt) > len(txt):
                                txt = final_txt
                        except Exception:
                            pass
                        try:
                            artifact_text = await self._extract_from_artifacts()
                            if artifact_text and len(artifact_text) > 30:
                                txt = txt + "\n\n" + artifact_text
                        except Exception:
                            pass
                        print(f"  {C.GREEN}  ✅ Generación completada tras espera extra "
                              f"({extra_waited}s) — {len(txt)} chars{C.RESET}")
                        return txt
                print(f"  {C.YELLOW}  ⚠️  Espera extra agotada — devolviendo lo que hay "
                      f"({len(txt)} chars){C.RESET}")
            else:
                print(f"  {C.YELLOW}  ⚠️  Timeout — devolviendo ({len(txt)} chars){C.RESET}")
            return txt

        # Último intento de rescate antes de devolver el mensaje de error.
        if self.site_key == "chatgpt":
            latest = await self._extract_latest_response()
            if latest and latest != prev_latest:
                print(f"  {C.YELLOW}  ⚠️  Timeout, usando fallback final ({len(latest)} chars){C.RESET}")
                return latest

        return "No se pudo leer la respuesta."

    # ══════════════════════════════════════════════════════════════════════════
    #  ENVIAR TEXTO AL INPUT — FIX 1: un solo intento de paste
    # ══════════════════════════════════════════════════════════════════════════

    async def _send_via_clipboard(self, el, text: str):
        pyperclip.copy(text)
        await el.click()
        await asyncio.sleep(0.3)
        await el.evaluate("el => el.focus()")
        mod = "Meta" if sys.platform == "darwin" else "Control"
        await el.press(f"{mod}+a")
        await asyncio.sleep(0.2)
        await el.press(f"{mod}+v")
        await asyncio.sleep(0.5)

    async def _send_via_evaluate(self, el, text: str):
        try:
            await el.evaluate(f"""
                (el) => {{
                    el.focus();
                    if (el.isContentEditable) {{
                        el.innerText = {repr(text)};
                        el.dispatchEvent(new Event('input', {{ bubbles: true }}));
                    }} else {{
                        const ns = Object.getOwnPropertyDescriptor(
                                       window.HTMLTextAreaElement.prototype, 'value')?.set;
                        if (ns) {{
                            ns.call(el, {repr(text)});
                            el.dispatchEvent(new Event('input',  {{ bubbles: true }}));
                            el.dispatchEvent(new Event('change', {{ bubbles: true }}));
                        }} else {{ el.value = {repr(text)}; }}
                    }}
                }})()
            """)
            await asyncio.sleep(1)
        except Exception as e:
            print(f"  {C.RED}  _send_via_evaluate falló: {e}{C.RESET}")

    # ══════════════════════════════════════════════════════════════════════════
    #  SEND PROMPT — método principal público
    # ══════════════════════════════════════════════════════════════════════════

    async def send_prompt(self, prompt: str) -> str:
        site = self.site
        page = self._page

        # Navegar al sitio si hace falta
        site_host = re.sub(r"^https?://", "", site["url"]).split("/")[0].lower()
        if site_host not in (page.url or "").lower():
            await page.goto(site["url"], wait_until="domcontentloaded", timeout=30000)
            await asyncio.sleep(3)

        # Login automático de ChatGPT (si está configurado)
        if (self.site_key == "chatgpt"
                and self._chatgpt_env_credentials_set()
                and self._chatgpt_allow_automated_login()):
            try:
                if await self._chatgpt_has_login_button():
                    await self.wait_for_login()
                    await page.goto(site["url"], wait_until="domcontentloaded", timeout=30000)
                    await asyncio.sleep(2)
            except Exception: pass

        # Esperar input
        try:
            await page.wait_for_selector(site["input_sel"], timeout=15000)
        except Exception as e:
            if await self.needs_login(navigate_if_needed=False):
                await self.wait_for_login()
                await page.goto(site["url"], wait_until="domcontentloaded", timeout=30000)
                await asyncio.sleep(2)
                await page.wait_for_selector(site["input_sel"], timeout=15000)
            else:
                print(f"  {C.RED}Error encontrando input: {e}{C.RESET}"); raise

        # ── Línea base ANTES de enviar ───────────────────────────────────────
        prev_count = await self._count_responses()
        prev_latest = await self._extract_latest_response()
        print(f"    Respuestas previas en DOM: {prev_count}")

        # ── Copiar prompt al portapapeles y pegarlo ──────────────────────────
        pyperclip.copy(prompt)
        print(f"    Prompt copiado al portapapeles ({len(prompt)} chars)")

        input_el = await _query_first(page, site["input_sel"])
        if not input_el:
            raise RuntimeError("No se encontró el elemento de input.")

        await self._send_via_clipboard(input_el, prompt)

        # Verificar que el texto llegó; si no, usar evaluate como fallback
        try:
            val = await input_el.evaluate(
                "el => el.isContentEditable ? el.innerText : el.value"
            )
            if not val or len(val.strip()) < len(prompt) * 0.5:
                print(f"  {C.YELLOW}  ⚠️  Clipboard no funcionó, usando evaluate...{C.RESET}")
                await self._send_via_evaluate(input_el, prompt)
        except Exception:
            pass

        # ── Enviar (click en botón) ──────────────────────────────────────────
        await asyncio.sleep(0.5)
        send_el = await _query_first(page, site["send_sel"])
        if send_el:
            await send_el.click()
        else:
            mod = "Meta" if sys.platform == "darwin" else "Control"
            await input_el.press("Enter")

        # ── Esperar respuesta ────────────────────────────────────────────────
        print(f"    Esperando respuesta de {site['name']}...")
        return await self._wait_for_response(prev_count=prev_count, prev_latest=prev_latest)


# ══════════════════════════════════════════════════════════════════════════════
#  SHUTDOWN — cierra sesiones activas globalmente
# ══════════════════════════════════════════════════════════════════════════════

_active_sessions: dict[str, BrowserSession] = {}

def get_or_create_session(site_key: str) -> BrowserSession:
    if site_key not in _active_sessions:
        _active_sessions[site_key] = BrowserSession(site_key)
    return _active_sessions[site_key]

def shutdown_ai_scraper_runtime():
    async def _close_all():
        for sess in _active_sessions.values():
            try:
                await sess.close()
            except Exception:
                pass
        _active_sessions.clear()
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            loop.create_task(_close_all())
        else:
            loop.run_until_complete(_close_all())
    except Exception:
        pass
