"""
sonny_ai.py — Asistente de automatización con lenguaje natural
Soporta múltiples proveedores de IA con fallback automático a modo sin modelo.
"""
import subprocess, os, sys, json, requests, difflib
from config import PROVIDERS, FALLBACK_NO_MODEL
from scan_apps import get_available_apps

# ── Colores ────────────────────────────────────────────────────────────────────
class C:
    CYAN   = "\033[96m"
    GREEN  = "\033[92m"
    YELLOW = "\033[93m"
    RED    = "\033[91m"
    BOLD   = "\033[1m"
    DIM    = "\033[2m"
    RESET  = "\033[0m"

APPS = get_available_apps()
SYSTEM_CMDS = {"notepad", "mspaint", "calc", "explorer", "cmd", "powershell"}
active_provider = None

# ══════════════════════════════════════════════════════════════════════════════
#   CAPA DE IA — múltiples proveedores
# ══════════════════════════════════════════════════════════════════════════════

def _system_prompt():
    apps_list = "\n".join(f"  - {n}" for n in APPS.keys())
    return f"""Eres el núcleo de un asistente de automatización llamado Sonny.
Interpreta la intención del usuario y devuelve SOLO un JSON válido, sin texto extra.

Items disponibles (apps, archivos, imágenes, videos, etc.):
{apps_list}

Formatos de respuesta:

Abrir algo de la lista (exacto o por descripción/sinónimo):
{{"action": "open_app", "app": "<nombre_exacto_de_la_lista>"}}

Posible error tipográfico (algo parecido existe):
{{"action": "suggest", "app": "<nombre_mas_cercano>", "message": "<ej: ¿Quisiste decir obs?>"}}

No existe y no hay nada parecido:
{{"action": "not_found", "message": "<explica brevemente en español>"}}

No entiendes la orden:
{{"action": "unknown", "message": "<pide clarificación en español>"}}

Saludo o pregunta sobre capacidades:
{{"action": "help", "message": "<respuesta corta listando los items>"}}

Ejemplos:
- "abre chrome"       -> {{"action": "open_app", "app": "chrome"}}
- "abre mi navegador" -> {{"action": "open_app", "app": "chrome"}}
- "abre ods"          -> {{"action": "suggest", "app": "obs", "message": "¿Quisiste decir obs?"}}
- "muéstrame mi foto" -> {{"action": "open_app", "app": "<nombre de la imagen>"}}
"""

def _call_openai_format(provider, user_input):
    headers = {"Authorization": f"Bearer {provider['api_key']}", "Content-Type": "application/json"}
    if "extra_headers" in provider:
        headers.update(provider["extra_headers"])
    resp = requests.post(
        provider["url"], headers=headers,
        json={
            "model": provider["model"],
            "messages": [
                {"role": "system", "content": _system_prompt()},
                {"role": "user",   "content": user_input},
            ],
            "temperature": 0.1, "max_tokens": 200,
        }, timeout=15,
    )
    resp.raise_for_status()
    return resp.json()["choices"][0]["message"]["content"].strip()

def _call_gemini(provider, user_input):
    url  = f"{provider['url']}?key={provider['api_key']}"
    resp = requests.post(url, json={
        "contents": [{"parts": [{"text": _system_prompt() + "\n\nUsuario: " + user_input}]}],
        "generationConfig": {"temperature": 0.1, "maxOutputTokens": 200},
    }, timeout=15)
    resp.raise_for_status()
    return resp.json()["candidates"][0]["content"]["parts"][0]["text"].strip()

def interpretar_con_ia(user_input):
    """Intenta todos los proveedores en orden. Devuelve dict o None si todos fallan."""
    global active_provider
    for provider in PROVIDERS:
        key = provider.get("api_key", "")
        if not key or "XXXX" in key:
            continue
        try:
            raw = _call_gemini(provider, user_input) if provider["format"] == "gemini" \
                  else _call_openai_format(provider, user_input)
            raw    = raw.replace("```json", "").replace("```", "").strip()
            result = json.loads(raw)
            active_provider = provider["name"]
            return result
        except requests.exceptions.HTTPError as e:
            code = e.response.status_code if e.response else 0
            labels = {401: "Key inválida ❌", 403: "Sin permisos ❌", 429: "Tokens/límite agotados ⏳"}
            msg = labels.get(code, f"Error HTTP {code}")
            print(f"{C.DIM}  [{provider['name']}] {msg}. Probando siguiente...{C.RESET}")
            if code == 401:
                print(f"{C.YELLOW}  ⚠️  Verifica tu API key de {provider['name']} en config.py{C.RESET}")
        except requests.exceptions.ConnectionError as e:
            err_str = str(e).lower()
            if "reset" in err_str or "refused" in err_str:
                print(f"{C.DIM}  [{provider['name']}] Conexión rechazada (¿firewall o VPN?).{C.RESET}")
            else:
                print(f"{C.DIM}  [{provider['name']}] Sin conexión a internet.{C.RESET}")
        except requests.exceptions.Timeout:
            print(f"{C.DIM}  [{provider['name']}] Timeout (servidor lento).{C.RESET}")
        except (json.JSONDecodeError, KeyError) as e:
            print(f"{C.DIM}  [{provider['name']}] Respuesta inesperada: {e}{C.RESET}")
    active_provider = None
    return None

# ══════════════════════════════════════════════════════════════════════════════
#   MODO SIN MODELO — fuzzy matching
# ══════════════════════════════════════════════════════════════════════════════

def interpretar_sin_modelo(user_input):
    txt   = user_input.lower().strip()
    names = list(APPS.keys())

    # 1. Nombre exacto dentro del texto
    for name in names:
        if name in txt:
            return {"action": "open_app", "app": name}

    # 2. Fuzzy sobre texto completo
    matches = difflib.get_close_matches(txt, names, n=1, cutoff=0.45)
    if matches:
        best  = matches[0]
        score = difflib.SequenceMatcher(None, txt, best).ratio()
        if score > 0.75:
            return {"action": "open_app", "app": best}
        return {"action": "suggest", "app": best,
                "message": f"¿Quisiste decir '{best}'? (modo sin IA)"}

    # 3. Fuzzy palabra por palabra
    for word in txt.split():
        if len(word) < 3:
            continue
        m2 = difflib.get_close_matches(word, names, n=1, cutoff=0.6)
        if m2:
            return {"action": "suggest", "app": m2[0],
                    "message": f"¿Quisiste decir '{m2[0]}'?"}

    return {"action": "not_found",
            "message": f"No encontré '{user_input}'.\n  Disponibles: {', '.join(names)}\n  Agrégalo con app_manager.py"}

# ══════════════════════════════════════════════════════════════════════════════
#   ABRIR — apps, imágenes, videos, documentos, cualquier archivo
# ══════════════════════════════════════════════════════════════════════════════

def abrir_item(name):
    ruta = APPS.get(name)
    if not ruta:
        print(f"{C.RED}❌ '{name}' no está en la lista.{C.RESET}")
        return
    try:
        if ruta in SYSTEM_CMDS:
            subprocess.Popen(ruta)
        elif ruta.lower().endswith(".exe"):
            try:
                subprocess.Popen(ruta)
            except Exception:
                os.startfile(ruta)
        else:
            # .lnk, imágenes, videos, pdfs, docs → Windows abre con la app por defecto
            os.startfile(ruta)
        print(f"{C.GREEN}✅ Abriendo '{name}'...{C.RESET}")
    except FileNotFoundError:
        print(f"{C.RED}❌ Archivo no encontrado: {ruta}")
        print(f"   Actualiza la ruta en app_manager.py{C.RESET}")
    except Exception as e:
        print(f"{C.RED}❌ Error: {e}{C.RESET}")

# ══════════════════════════════════════════════════════════════════════════════
#   EJECUTAR ACCIÓN
# ══════════════════════════════════════════════════════════════════════════════

def ejecutar(accion):
    """Devuelve nombre sugerido si espera confirmación, o None."""
    action = accion.get("action")
    if action == "open_app":
        abrir_item(accion.get("app", ""))
    elif action == "suggest":
        app_sug = accion.get("app", "")
        print(f"{C.YELLOW}🤖 Sonny: {accion.get('message', f'¿Quisiste decir {app_sug}?')} {C.DIM}(s/n){C.RESET}")
        return app_sug
    elif action in ("not_found", "unknown", "help"):
        print(f"{C.YELLOW}🤖 Sonny: {accion.get('message', '')}{C.RESET}")
    elif action == "error":
        print(f"{C.RED}⚠️  {accion.get('message', 'Error')}{C.RESET}")
    else:
        print(f"{C.RED}❌ Acción desconocida: {action}{C.RESET}")
    return None

# ══════════════════════════════════════════════════════════════════════════════
#   BANNER
# ══════════════════════════════════════════════════════════════════════════════

TIPO_EXT = {".exe":"app",".lnk":"acceso",".jpg":"imagen",".jpeg":"imagen",".png":"imagen",
            ".gif":"imagen",".mp4":"video",".mkv":"video",".avi":"video",
            ".mp3":"audio",".wav":"audio",".pdf":"doc",".docx":"doc",".xlsx":"doc"}

def tipo_item(path):
    if path in SYSTEM_CMDS:
        return "sistema"
    return TIPO_EXT.get(os.path.splitext(path)[1].lower(), "archivo")

def banner():
    keys_ok = sum(1 for p in PROVIDERS if p.get("api_key") and "XXXX" not in p["api_key"])
    ai_st   = (f"{C.GREEN}{keys_ok} proveedor(es) configurado(s){C.RESET}" if keys_ok
               else f"{C.YELLOW}sin proveedores — modo sin modelo activo{C.RESET}")
    print(f"""
{C.CYAN}{C.BOLD}  ╔═══════════════════════════════╗
  ║        SONNY  AI  v3.0        ║
  ╚═══════════════════════════════╝{C.RESET}
  {C.DIM}IA: {ai_st}
  Items: {len(APPS)} cargados{C.RESET}

  {C.YELLOW}Habla con Sonny en tus propias palabras.{C.RESET}
  {C.DIM}Puedes abrir apps, imágenes, videos, documentos...
  Escribe 'lista' para ver todo  |  'salir' para cerrar{C.RESET}
    """)

# ══════════════════════════════════════════════════════════════════════════════
#   LOOP PRINCIPAL
# ══════════════════════════════════════════════════════════════════════════════

# Palabras que dentro de una frase significan "sí"
PALABRAS_SI  = {"s","si","sí","yes","y","ok","dale","claro","correcto",
                "exacto","abrelo","ábrelo","abrir","anda","venga","hazlo",
                "adelante","confirmo","confirmar","afirmativo","obvio",
                "porfa","porfavor","please","abrir","abre","lanza","ejecuta"}
# Palabras que dentro de una frase significan "no"
PALABRAS_NO  = {"no","nope","nel","negativo","cancela","cancelar","olvida",
                "olvídalo","mejor no","para","detente"}

def es_afirmativo(texto: str) -> bool:
    """True si el texto contiene alguna palabra afirmativa (aunque haya más palabras)."""
    palabras = set(texto.lower().split())
    if palabras & PALABRAS_NO:      # primero revisar negativas
        return False
    return bool(palabras & PALABRAS_SI)

if __name__ == "__main__":
    banner()
    pendiente  = None
    modo_no_ai = not any(p.get("api_key") and "XXXX" not in p["api_key"] for p in PROVIDERS)

    while True:
        try:
            prefijo    = f"{C.YELLOW}[sin IA]{C.RESET} " if modo_no_ai else ""
            user_input = input(f"{prefijo}{C.CYAN}{C.BOLD}tú > {C.RESET}").strip()

            if not user_input:
                continue
            low = user_input.lower()

            if low in ("salir", "exit", "quit", "chau"):
                print(f"{C.CYAN}👋 Hasta luego.{C.RESET}")
                break

            if low == "debug":
                print(f"\n{C.BOLD}Testeando proveedores...{C.RESET}")
                for provider in PROVIDERS:
                    key = provider.get("api_key", "")
                    if not key or "XXXX" in key:
                        print(f"  {C.DIM}[{provider['name']}] Sin configurar (XXXX){C.RESET}")
                        continue
                    print(f"  [{provider['name']}] Probando...", end=" ", flush=True)
                    try:
                        if provider["format"] == "gemini":
                            raw = _call_gemini(provider, "hola")
                        else:
                            raw = _call_openai_format(provider, "hola")
                        print(f"{C.GREEN}✅ OK{C.RESET}")
                        if modo_no_ai:
                            modo_no_ai = False
                            print(f"  {C.GREEN}Modo IA restaurado con {provider['name']}{C.RESET}")
                    except Exception as e:
                        print(f"{C.RED}❌ {type(e).__name__}: {e}{C.RESET}")
                print()
                continue
                print(f"\n{C.BOLD}Items disponibles:{C.RESET}")
                for n, p in sorted(APPS.items()):
                    print(f"  {C.GREEN}•{C.RESET} {n:<24} {C.DIM}[{tipo_item(p)}]  {p}{C.RESET}")
                print()
                continue

            # ── Confirmación pendiente ─────────────────────────────────────
            if pendiente:
                if es_afirmativo(low):
                    abrir_item(pendiente)
                else:
                    print(f"{C.DIM}  Cancelado.{C.RESET}")
                pendiente = None
                continue

            # ── Intentar con IA ────────────────────────────────────────────
            accion = None
            if not modo_no_ai:
                lbl = active_provider or "IA"
                print(f"{C.DIM}  [interpretando con {lbl}...]{C.RESET}", end="\r")
                accion = interpretar_con_ia(user_input)
                print(" " * 45, end="\r")

                if accion is None:
                    if FALLBACK_NO_MODEL:
                        modo_no_ai = True
                        print(f"{C.YELLOW}⚠️  Todos los proveedores fallaron.")
                        print(f"   Modo sin modelo activado — escribe el nombre exacto o similar.{C.RESET}\n")
                    else:
                        print(f"{C.RED}❌ Sin IA. Configura un proveedor en config.py{C.RESET}")
                        continue

            if modo_no_ai or accion is None:
                accion = interpretar_sin_modelo(user_input)

            pendiente = ejecutar(accion)

        except KeyboardInterrupt:
            print(f"\n{C.CYAN}👋 Hasta luego.{C.RESET}")
            break
