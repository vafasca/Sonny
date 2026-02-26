"""
core/browser.py — Controla el navegador con Playwright.
Maneja sesiones persistentes para no re-loguearse cada vez.
USA PORTAPAPELES (pyperclip) para enviar prompts largos sin truncar.
"""
import asyncio, os, sys, time, re
import pyperclip
from pathlib import Path
from typing import Optional

# ── Colores ────────────────────────────────────────────────────────────────────
class C:
    CYAN="\033[96m"; GREEN="\033[92m"; YELLOW="\033[93m"
    RED="\033[91m";  BOLD="\033[1m";   DIM="\033[2m"; RESET="\033[0m"

# ── Rutas de sesión ────────────────────────────────────────────────────────────
SESSIONS_DIR = Path(__file__).parent.parent / "sessions"
SESSIONS_DIR.mkdir(exist_ok=True)

# ── Configuración de cada IA ───────────────────────────────────────────────────
AI_SITES = {
    "claude": {
        "url":          "https://claude.ai/new",
        "name":         "Claude.ai",
        "input_sel":    '[contenteditable="true"]',
        "send_sel":     'button[aria-label="Send message"]',
        "response_sel": ".font-claude-message",
        "done_sel":     'button[aria-label="Send message"]:not([disabled])',
        "session_file": "claude_session",
    },
    "chatgpt": {
        "url":          "https://chatgpt.com/",
        "name":         "ChatGPT",
        "input_sel":    '#prompt-textarea',
        "send_sel":     'button[data-testid="send-button"]',
        "response_sel": '[data-message-author-role="assistant"]',
        "done_sel":     'button[data-testid="send-button"]:not([disabled])',
        "session_file": "chatgpt_session",
    },
    "gemini": {
        "url":          "https://gemini.google.com/app",
        "name":         "Gemini",
        "input_sel":    ".ql-editor",
        "send_sel":     'button[aria-label="Send message"]',
        "response_sel": ".model-response-text",
        "done_sel":     'button[aria-label="Send message"]:not([disabled])',
        "session_file": "gemini_session",
    },
    "qwen": {
        "url":          "https://chat.qwen.ai/",
        "name":         "Qwen",
        "input_sel":    'textarea',
        "send_sel":     'button[type="submit"]',
        "response_sel": ".markdown-body",
        "done_sel":     'button[type="submit"]:not([disabled])',
        "session_file": "qwen_session",
    },
}

def check_playwright() -> bool:
    """Verifica si playwright está instalado."""
    try:
        import playwright
        return True
    except ImportError:
        return False

def install_playwright():
    """Instala playwright si no está disponible."""
    import subprocess
    print(f"  {C.YELLOW}Instalando Playwright...{C.RESET}")
    subprocess.run([sys.executable, "-m", "pip", "install", "playwright",
                    "--break-system-packages", "-q"], check=True)
    subprocess.run([sys.executable, "-m", "playwright", "install", "chromium"], check=True)
    print(f"  {C.GREEN}✅ Playwright instalado{C.RESET}")


class BrowserSession:
    """Sesión de navegador persistente para una IA específica."""

    def __init__(self, site_key: str):
        if site_key not in AI_SITES:
            raise ValueError(f"Sitio desconocido: {site_key}. Opciones: {list(AI_SITES.keys())}")
        self.site     = AI_SITES[site_key]
        self.site_key = site_key
        self.session_path = SESSIONS_DIR / self.site["session_file"]
        self._browser     = None
        self._context     = None
        self._page        = None
        self._pw          = None
        self._started     = False

    async def start(self):
        """Inicia la sesión del navegador (si aún no está iniciada)."""
        if self._started:
            return self

        from playwright.async_api import async_playwright
        self._pw      = await async_playwright().start()
        self._browser = await self._pw.chromium.launch(
            headless=False,
            args=["--start-maximized"]
        )
        self._context = await self._browser.new_context(
            storage_state=str(self.session_path) if self.session_path.exists() else None,
            viewport={"width": 1280, "height": 800},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            )
        )
        self._page = await self._context.new_page()

        # Dar permisos de clipboard al contexto para que Ctrl+V funcione
        await self._context.grant_permissions(["clipboard-read", "clipboard-write"])
        self._started = True
        return self

    async def close(self):
        """Cierra la sesión y guarda estado si está iniciada."""
        if not self._started:
            return

        if self._context:
            await self._context.storage_state(path=str(self.session_path))
        if self._browser:
            await self._browser.close()
        if self._pw:
            await self._pw.stop()

        self._browser = None
        self._context = None
        self._page = None
        self._pw = None
        self._started = False

    async def __aenter__(self):
        return await self.start()

    async def __aexit__(self, *args):
        await self.close()

    async def needs_login(self) -> bool:
        """Detecta si la página pide login."""
        await self._page.goto(self.site["url"], wait_until="domcontentloaded", timeout=30000)
        await asyncio.sleep(2)
        url = self._page.url.lower()
        login_signals = ["login", "signin", "sign-in", "auth", "account/login", "welcome"]
        return any(s in url for s in login_signals)

    async def wait_for_login(self):
        """Espera a que el usuario se loguee manualmente."""
        print(f"\n  {C.YELLOW}{'─'*50}{C.RESET}")
        print(f"  {C.YELLOW}🔐 Necesitas loguearte en {self.site['name']}{C.RESET}")
        print(f"  {C.DIM}El navegador está abierto. Loguéate y luego vuelve aquí.{C.RESET}")
        print(f"  {C.CYAN}Presiona ENTER cuando hayas iniciado sesión...{C.RESET}")
        input()
        await self._context.storage_state(path=str(self.session_path))
        print(f"  {C.GREEN}✅ Sesión guardada en {self.session_path}{C.RESET}\n")

    # ──────────────────────────────────────────────────────────────────────────
    #   ENVIAR PROMPT — usa portapapeles para soportar texto de cualquier largo
    # ──────────────────────────────────────────────────────────────────────────

    async def send_prompt(self, prompt: str) -> str:
        """
        Envía el prompt completo a la IA usando Ctrl+V (portapapeles).
        Soporta prompts de cualquier longitud sin truncar.
        """
        site = self.site
        page = self._page

        # Mantener chat actual: solo navegar al sitio si la pestaña no está en la IA.
        current_url = (page.url or "").lower()
        site_host = re.sub(r"^https?://", "", site["url"]).split("/")[0].lower()
        if site_host not in current_url:
            await page.goto(site["url"], wait_until="domcontentloaded", timeout=30000)
            await asyncio.sleep(3)

        # Esperar el input
        try:
            await page.wait_for_selector(site["input_sel"], timeout=15000)
        except Exception as e:
            print(f"  {C.RED}Error encontrando input: {e}{C.RESET}")
            raise

        # ── Copiar al portapapeles del sistema con pyperclip ──────────────────
        try:
            pyperclip.copy(prompt)
            print(f"  {C.DIM}  Prompt copiado al portapapeles ({len(prompt)} chars){C.RESET}")
        except Exception as e:
            print(f"  {C.YELLOW}  ⚠️  pyperclip falló: {e} — usando método directo{C.RESET}")
            await self._send_via_evaluate(prompt)
            return await self._wait_for_response()

        # ── Click en el input y pegar ─────────────────────────────────────────
        await page.click(site["input_sel"])
        await asyncio.sleep(0.5)

        # Limpiar cualquier texto previo
        await page.keyboard.press("Control+a")
        await asyncio.sleep(0.2)
        await page.keyboard.press("Delete")
        await asyncio.sleep(0.3)

        # Pegar con Ctrl+V
        await page.keyboard.press("Control+v")
        await asyncio.sleep(2)  # esperar a que pegue todo (prompts largos necesitan más)

        # ── Verificar que llegó completo ──────────────────────────────────────
        ok = await self._verify_paste(prompt)
        if not ok:
            print(f"  {C.YELLOW}  ⚠️  Pegado incompleto, reintentando...{C.RESET}")
            await page.keyboard.press("Control+a")
            await page.keyboard.press("Delete")
            await asyncio.sleep(0.5)
            pyperclip.copy(prompt)
            await page.keyboard.press("Control+v")
            await asyncio.sleep(2.5)

            # Segundo intento fallido → fallback con evaluate
            ok = await self._verify_paste(prompt)
            if not ok:
                print(f"  {C.YELLOW}  ⚠️  Usando método alternativo...{C.RESET}")
                await page.keyboard.press("Control+a")
                await page.keyboard.press("Delete")
                await asyncio.sleep(0.3)
                await self._send_via_evaluate(prompt)

        # ── Enviar con botón o Enter ──────────────────────────────────────────
        await asyncio.sleep(0.5)
        try:
            send_btn = await page.query_selector(site["send_sel"])
            if send_btn:
                await send_btn.click()
            else:
                await page.keyboard.press("Enter")
        except Exception:
            await page.keyboard.press("Enter")

        print(f"  {C.DIM}  Esperando respuesta de {site['name']}...{C.RESET}")
        return await self._wait_for_response()

    async def _verify_paste(self, prompt: str) -> bool:
        """
        Verifica que el texto pegado en el input tenga al menos el 80% del prompt.
        Devuelve True si está completo, False si está truncado.
        """
        try:
            content = await self._page.evaluate(f"""
                (() => {{
                    const el = document.querySelector('{self.site["input_sel"]}');
                    if (!el) return '';
                    return el.innerText || el.value || el.textContent || '';
                }})()
            """)
            ratio = len(content.strip()) / max(len(prompt), 1)
            if ratio < 0.80:
                print(f"  {C.DIM}  Verificación: {len(content)}/{len(prompt)} chars ({ratio:.0%}){C.RESET}")
                return False
            return True
        except Exception:
            return True  # si no podemos verificar, asumir OK

    async def _send_via_evaluate(self, prompt: str):
        """
        Método alternativo: inserta el texto via JavaScript directamente.
        Funciona para la mayoría de inputs aunque el prompt sea largo.
        """
        site = self.site
        page = self._page
        try:
            await page.evaluate(f"""
                (() => {{
                    const el = document.querySelector('{site["input_sel"]}');
                    if (!el) return;
                    el.focus();
                    const text = {repr(prompt)};
                    if (el.tagName === 'TEXTAREA' || el.tagName === 'INPUT') {{
                        const nativeSetter = Object.getOwnPropertyDescriptor(
                            window.HTMLTextAreaElement.prototype, 'value'
                        )?.set;
                        if (nativeSetter) {{
                            nativeSetter.call(el, text);
                            el.dispatchEvent(new Event('input', {{ bubbles: true }}));
                            el.dispatchEvent(new Event('change', {{ bubbles: true }}));
                        }} else {{
                            el.value = text;
                        }}
                    }} else {{
                        // contenteditable (ChatGPT, Claude)
                        el.focus();
                        document.execCommand('selectAll');
                        document.execCommand('insertText', false, text);
                        // Fallback si execCommand no funciona
                        if (!el.innerText || el.innerText.length < text.length * 0.5) {{
                            el.innerText = text;
                            el.dispatchEvent(new Event('input', {{ bubbles: true }}));
                        }}
                    }}
                }})()
            """)
            await asyncio.sleep(1)
        except Exception as e:
            print(f"  {C.RED}  _send_via_evaluate falló: {e}{C.RESET}")

    # ──────────────────────────────────────────────────────────────────────────
    #   ESPERAR RESPUESTA
    # ──────────────────────────────────────────────────────────────────────────

    async def _wait_for_response(self, max_wait: int = 120) -> str:
        """Espera a que la IA termine de responder y devuelve el texto."""
        site = self.site
        page = self._page

        # Paso 1: Esperar a que aparezca alguna respuesta
        try:
            await page.wait_for_selector(site["response_sel"], timeout=30000)
        except Exception:
            print(f"  {C.YELLOW}  Selector de respuesta no encontrado, esperando...{C.RESET}")
            await asyncio.sleep(5)

        # Paso 2: Esperar a que el texto se estabilice (IA terminó de generar)
        start       = time.time()
        last_text   = ""
        stable_count = 0

        while time.time() - start < max_wait:
            await asyncio.sleep(2)

            try:
                elements = await page.query_selector_all(site["response_sel"])
                if elements:
                    current_text = await elements[-1].inner_text()
                    current_text = current_text.strip()

                    if current_text == last_text and current_text:
                        stable_count += 1
                        if stable_count >= 3:   # 6 segundos sin cambios → terminó
                            return current_text
                    else:
                        stable_count = 0
                        last_text    = current_text
            except Exception:
                pass

            # También verificar si el botón de enviar está habilitado de nuevo
            try:
                send_btn = await page.query_selector(site["done_sel"])
                if send_btn and last_text:
                    stable_count += 1
                    if stable_count >= 2:
                        await asyncio.sleep(2)
                        elements = await page.query_selector_all(site["response_sel"])
                        if elements:
                            return (await elements[-1].inner_text()).strip()
            except Exception:
                pass

        return last_text or "No se pudo leer la respuesta."
