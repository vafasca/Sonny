"""
browser.py — v10  (Fix ChatGPT extracción de código + umbral de detección)
══════════════════════════════════════════════════════════════════════════════
HISTORIAL DE FIXES
──────────────────────────────────────────────────────────────────────────────
FIX 1 — PASTE LIMPIO:
  PROBLEMA: Al pegar texto largo en el textarea, a veces se duplicaba.
  SOLUCIÓN: Un solo intento de paste vía pyperclip + clipboard.

FIX 2 — RESPUESTA NUEVA (render-count, SOLO para Claude):
  PROBLEMA: multi-turno devolvía siempre la misma respuesta (la anterior).
  SOLUCIÓN: contar bloques antes de enviar, esperar uno nuevo.

FIX 3 — TIMEOUT EXTENDIDO + ESPERA EXTRA (SOLO para Claude):
  PROBLEMA: Claude con razonamiento extendido puede tardar 3-5 min.
  SOLUCIÓN: max_wait=360s + loop extra de hasta 120s si sigue generando.

FIX 4 — EXTRACCIÓN POR-IA TOTALMENTE AISLADA (v9):
  PROBLEMA: _COUNT_RENDERS_JS y _EXTRACT_NEW_RESPONSE_JS usaban
  [data-test-render-count], un selector exclusivo de Claude.
  SOLUCIÓN: Cada entrada de AI_SITES tiene sus propios "count_js" y "extract_js".

FIX 5 — CHATGPT: SELECTOR [class*="action"] DEMASIADO AMPLIO (v10):
  PROBLEMA RAÍZ 1 — código en una sola línea:
    El selector `[class*="action"]` eliminaba divs contenedores de bloques
    de código de ChatGPT (usan clases como "code-action-bar", "actions").
    Resultado: pre.textContent devolvía string vacío → innerText colapsaba
    todo el bloque en una línea sin separadores.

  PROBLEMA RAÍZ 2 — ng new no capturado (timeout 180s):
    El mismo selector eliminaba el bloque que contenía el comando `ng new`.
    `current` siempre devolvía "" → len("") <= 3 → no_text_count++  hasta
    timeout → retornaba "No se pudo leer la respuesta." (29 chars).

  SOLUCIÓN:
    · Reemplazar `button, [class*="action"], [class*="feedback"]` por
      selectores ESPECÍFICOS de los botones de ChatGPT (data-testid exactos).
    · Procesar <br> DENTRO de <pre> ANTES de extraer textContent.
    · Añadir manejo de spans de línea (syntax highlighters que usan
      display:block via CSS en lugar de \n en text nodes).
    · Bajar umbral de detección de `len > 40` a `len > 3`.
    · Aumentar paciencia de `no_text_count >= 12` a `>= 20`.
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
#  CONFIGURACIÓN DE SITIOS
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

        # ── count_js: cuenta respuestas completas (no estados "pensando") ─────
        "count_js": r"""
            () => {
                return document.querySelectorAll(
                    '[data-message-author-role="assistant"]'
                ).length;
            }
        """,

        # ── extract_js v10: FIX selector action + br-inside-pre ──────────────
        #
        #  CAMBIOS vs v9:
        #  1. Reemplaza `button, [class*="action"], [class*="feedback"]` por
        #     data-testid EXACTOS → ya no elimina contenedores de código.
        #  2. Procesa <br> DENTRO de <pre> ANTES de leer textContent.
        #  3. Maneja spans de línea (syntax highlighters sin \n en text nodes).
        #  4. Fallback a textContent completo si innerText devuelve muy poco.
        #
        "extract_js": r"""
            (prevCount) => {
                const msgs = Array.from(
                    document.querySelectorAll('[data-message-author-role="assistant"]')
                );
                if (msgs.length <= prevCount) return '';

                const newest = msgs[msgs.length - 1];

                // ── Verificación rápida: hay contenido real? ──────────────────
                // Usamos el DOM VIVO para verificar antes de clonar
                const quickText = (newest.innerText || newest.textContent || '').trim();
                if (!quickText || quickText.length < 2) return '';

                const clone = newest.cloneNode(true);

                // ── FIX 5a: Eliminar SOLO botones específicos de ChatGPT ──────
                // ANTES (problemático): 'button, [class*="action"], [class*="feedback"]'
                // Eso eliminaba divs contenedores de bloques de código.
                // AHORA: solo data-testid exactos de botones de UI.
                clone.querySelectorAll([
                    '[data-testid="copy-turn-action-button"]',
                    '[data-testid="thumbs-up-button"]',
                    '[data-testid="thumbs-down-button"]',
                    '[data-testid="voice-play-turn-action-button"]',
                    '[data-testid="regenerate-button"]',
                    '[data-testid="read-aloud-turn-action-button"]',
                    // Botón de copiar dentro del bloque de código (el icono, no el contenido)
                    '.code-block__copy-button',
                    '.copybtn',
                    'button[title="Copy"]',
                    'button[aria-label="Copy"]',
                    'button[aria-label="Copiar"]',
                ].join(', ')).forEach(el => el.remove());

                // ── FIX 5b: Procesar bloques <pre> preservando newlines ───────
                //
                // ORDEN CRÍTICO: primero convertir <br> DENTRO del <pre>,
                // luego añadir \n en spans de línea, luego extraer textContent.
                // En v9, los <br> globales se procesaban DESPUÉS de que el <pre>
                // ya había sido reemplazado → los <br> internos quedaban perdidos.
                //
                clone.querySelectorAll('pre').forEach(pre => {

                    // Paso A: <br> dentro del <pre> → \n explícito
                    pre.querySelectorAll('br').forEach(br => {
                        br.parentNode.replaceChild(
                            document.createTextNode('\n'), br
                        );
                    });

                    // Paso B: Syntax highlighters que usan spans con display:block
                    // (PrismJS, highlight.js, ChatGPT custom) → añadir \n al final
                    // de cada span de línea para que textContent los incluya.
                    // Selectores comunes: .line, [class*="line "], token-line, etc.
                    const lineSpans = pre.querySelectorAll(
                        '.line, [class*=" line"], [class^="line"], ' +
                        '.token-line, .code-line, [data-line]'
                    );
                    if (lineSpans.length > 1) {
                        lineSpans.forEach(span => {
                            const lastChild = span.lastChild;
                            // Solo añadir \n si no termina ya con uno
                            if (!lastChild ||
                                lastChild.nodeType !== Node.TEXT_NODE ||
                                !lastChild.textContent.endsWith('\n')) {
                                span.appendChild(document.createTextNode('\n'));
                            }
                        });
                    }

                    // Paso C: Extraer textContent (ahora incluye \n de A y B)
                    const rawText = pre.textContent;
                    const withNewlines = '\n' + rawText + '\n';
                    pre.parentNode.replaceChild(
                        document.createTextNode(withNewlines), pre
                    );
                });

                // ── FIX 5c: <br> fuera de <pre> (ya procesados los internos) ──
                clone.querySelectorAll('br').forEach(br => {
                    br.parentNode.replaceChild(
                        document.createTextNode('\n'), br
                    );
                });

                // ── FIX 5d: <code> inline sin <pre> padre ─────────────────────
                clone.querySelectorAll('code').forEach(code => {
                    // Si el code YA estaba dentro de un pre, fue procesado arriba
                    // y el pre fue reemplazado → este code ya no tiene padre pre
                    // Solo procesar codes que aún existan en el clone
                    if (code.isConnected && code.closest('pre') === null) {
                        const raw = code.textContent;
                        if (code.parentNode) {
                            code.parentNode.replaceChild(
                                document.createTextNode(raw), code
                            );
                        }
                    }
                });

                const result = clone.innerText.trim();

                // ── Fallback: si innerText devuelve muy poco pero quickText ──
                // tiene bastante, usar textContent del clone como alternativa
                if (result.length < 10 && quickText.length > result.length * 2) {
                    // textContent no aplica CSS pero devuelve todo el texto
                    const fallback = clone.textContent.trim();
                    if (fallback.length > result.length) {
                        return fallback;
                    }
                }

                return result;
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
        self._started             = False
        self._using_system_chrome = False
        self._profile_dir = EDGE_PROFILES_DIR / f"profile_{site_key}"
        self._profile_dir.mkdir(exist_ok=True)

    # ── Ciclo de vida ──────────────────────────────────────────────────────────

    async def _start_with_system_chrome(self) -> bool:
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
        if self._started:
            return self

        if not check_playwright():
            install_playwright()

        from playwright.async_api import async_playwright
        self._pw = await async_playwright().start()

        if not await self._start_with_system_chrome():
            self._context = await self._pw.chromium.launch_persistent_context(
                str(self._profile_dir),
                channel="msedge",
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

    # ── Extracción por-IA ──────────────────────────────────────────────────────

    async def _count_responses(self) -> int:
        try:
            count = await self._page.evaluate(self.site["count_js"])
            return int(count or 0)
        except Exception:
            return 0

    async def _extract_new_response(self, prev_count: int) -> str:
        try:
            texto = await self._page.evaluate(self.site["extract_js"], prev_count)
            return (texto or "").strip()
        except Exception:
            return ""

    # ── Detección de generación activa ────────────────────────────────────────

    async def _is_generating(self) -> bool:
        page = self._page
        site = self.site
        for sel in site["done_sel"].split(","):
            sel = sel.strip()
            if not sel: continue
            try:
                btn = await page.query_selector(sel)
                if btn: return False
            except Exception: continue
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
    #  WAIT FOR RESPONSE — v10
    #
    #  CAMBIOS vs v9:
    #  · Umbral de detección: len > 40  →  len > 3
    #    Motivo: respuestas cortas como "ng new ..." (< 40 chars) nunca
    #    superaban el umbral → no_text_count++ hasta timeout.
    #  · no_text_count >= 12  →  >= 20
    #    Más paciencia antes de rendirse cuando last_txt es vacío.
    #  · Log de diagnóstico mejorado.
    # ══════════════════════════════════════════════════════════════════════════

    async def _wait_for_response(self, max_wait: int = 0,
                                  prev_count: int = 0) -> str:
        if max_wait == 0:
            max_wait = 360 if self.site_key == "claude" else 180

        start         = time.time()
        last_txt      = ""
        stable        = 0
        no_text_count = 0
        new_appeared  = False

        await asyncio.sleep(3)

        while time.time() - start < max_wait:
            await asyncio.sleep(2)

            try:
                current = await self._extract_new_response(prev_count)
            except Exception:
                current = ""

            # ── FIX 5e: Umbral bajado de > 40 a > 3 ─────────────────────────
            # Antes: respuestas cortas (comandos de 1 línea) nunca superaban
            # el umbral → se iban directo a no_text_count++ → timeout.
            if current and len(current) > 3:
                no_text_count = 0
                if not new_appeared:
                    new_appeared = True
                    print(f"  {C.DIM}  Nueva respuesta detectada ({len(current)} chars)...{C.RESET}")

                if current == last_txt:
                    stable += 1
                    if stable >= 3:
                        if not await self._is_generating():
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
            else:
                no_text_count += 1
                if no_text_count == 8:
                    elapsed = int(time.time() - start)
                    print(f"  {C.YELLOW}  ⚠️  Aún esperando nueva respuesta... ({elapsed}s){C.RESET}")
                # ── FIX 5e: Paciencia aumentada de >= 12 a >= 20 ─────────────
                # Más tiempo antes de devolver last_txt cuando es vacío.
                if no_text_count >= 20 and last_txt:
                    if not await self._is_generating():
                        return last_txt

        # ── Timeout ───────────────────────────────────────────────────────────
        txt = last_txt
        if txt:
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

        return "No se pudo leer la respuesta."

    # ══════════════════════════════════════════════════════════════════════════
    #  ENVIAR TEXTO AL INPUT
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

        site_host = re.sub(r"^https?://", "", site["url"]).split("/")[0].lower()
        if site_host not in (page.url or "").lower():
            await page.goto(site["url"], wait_until="domcontentloaded", timeout=30000)
            await asyncio.sleep(3)

        if (self.site_key == "chatgpt"
                and self._chatgpt_env_credentials_set()
                and self._chatgpt_allow_automated_login()):
            try:
                if await self._chatgpt_has_login_button():
                    await self.wait_for_login()
                    await page.goto(site["url"], wait_until="domcontentloaded", timeout=30000)
                    await asyncio.sleep(2)
            except Exception: pass

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

        prev_count = await self._count_responses()
        print(f"    Respuestas previas en DOM: {prev_count}")

        pyperclip.copy(prompt)
        print(f"    Prompt copiado al portapapeles ({len(prompt)} chars)")

        input_el = await _query_first(page, site["input_sel"])
        if not input_el:
            raise RuntimeError("No se encontró el elemento de input.")

        await self._send_via_clipboard(input_el, prompt)

        try:
            val = await input_el.evaluate(
                "el => el.isContentEditable ? el.innerText : el.value"
            )
            if not val or len(val.strip()) < len(prompt) * 0.5:
                print(f"  {C.YELLOW}  ⚠️  Clipboard no funcionó, usando evaluate...{C.RESET}")
                await self._send_via_evaluate(input_el, prompt)
        except Exception:
            pass

        await asyncio.sleep(0.5)
        send_el = await _query_first(page, site["send_sel"])
        if send_el:
            await send_el.click()
        else:
            mod = "Meta" if sys.platform == "darwin" else "Control"
            await input_el.press("Enter")

        print(f"    Esperando respuesta de {site['name']}...")
        return await self._wait_for_response(prev_count=prev_count)


# ══════════════════════════════════════════════════════════════════════════════
#  SHUTDOWN
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