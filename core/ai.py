"""
core/ai.py — Capa de inteligencia de Sonny.
Intenta múltiples proveedores en orden; si todos fallan usa fuzzy matching.
"""
import json, difflib, requests, ssl
from config import PROVIDERS, FALLBACK_NO_MODEL
from core.registry import get_all

# ── Estado ─────────────────────────────────────────────────────────────────────
active_provider: str | None = None

# ── System prompt ──────────────────────────────────────────────────────────────
def _prompt() -> str:
    items = "\n".join(f"  - {n}" for n in get_all().keys())
    return f"""Eres el núcleo de Sonny, un asistente de automatización.
Interpreta la intención del usuario y devuelve SOLO JSON válido, sin texto extra.

Items disponibles:
{items}

Respuestas posibles:

Abrir algo (exacto, sinónimo o descripción):
{{"action":"open","item":"<nombre_exacto>"}}

Probable typo (algo parecido existe):
{{"action":"suggest","item":"<nombre_cercano>","msg":"<ej: ¿Quisiste decir obs?>"}}

No existe en la lista:
{{"action":"not_found","msg":"<explica en español>"}}

No entiendes:
{{"action":"unknown","msg":"<pide clarificación>"}}

Saludo/ayuda:
{{"action":"help","msg":"<respuesta corta en español>"}}

Reglas:
- Usa SOLO nombres de la lista, nunca inventes nombres.
- Si el texto tiene un error tipográfico obvio, usa "suggest".
- "abre mi navegador" → chrome, "quiero grabar" → obs, "editor de código" → vscode.
"""

# ── Llamadas a proveedores ─────────────────────────────────────────────────────

def _call_openai(provider: dict, text: str) -> str:
    headers = {
        "Authorization": f"Bearer {provider['api_key']}",
        "Content-Type":  "application/json",
        **(provider.get("extra_headers") or {}),
    }
    r = requests.post(
        provider["url"], headers=headers,
        json={
            "model":    provider["model"],
            "messages": [
                {"role": "system", "content": _prompt()},
                {"role": "user",   "content": text},
            ],
            "temperature": 0.1,
            "max_tokens":  150,
        },
        timeout=12,
    )
    r.raise_for_status()
    return r.json()["choices"][0]["message"]["content"].strip()

def _call_gemini(provider: dict, text: str) -> str:
    url = f"{provider['url']}?key={provider['api_key']}"
    r   = requests.post(url, json={
        "contents": [{"parts": [{"text": _prompt() + "\n\nUsuario: " + text}]}],
        "generationConfig": {"temperature": 0.1, "maxOutputTokens": 150},
    }, timeout=12)
    r.raise_for_status()
    return r.json()["candidates"][0]["content"]["parts"][0]["text"].strip()

def _parse(raw: str) -> dict:
    clean = raw.replace("```json", "").replace("```", "").strip()
    return json.loads(clean)

# ── Errores descriptivos ───────────────────────────────────────────────────────

_HTTP_LABELS = {
    400: "Solicitud inválida",
    401: "API key inválida ❌ — revisa config.py",
    403: "Sin permisos ❌",
    429: "Límite de tokens/requests agotado ⏳",
    500: "Error interno del servidor",
    503: "Servicio no disponible",
}

def _describe_error(e: Exception, name: str) -> str:
    if isinstance(e, requests.exceptions.SSLError):
        return f"[{name}] Error SSL — posible antivirus o proxy interceptando HTTPS"
    if isinstance(e, requests.exceptions.ConnectionError):
        msg = str(e).lower()
        if "reset" in msg or "refused" in msg:
            return f"[{name}] Conexión rechazada — ¿firewall, VPN o antivirus bloqueando?"
        return f"[{name}] Sin conexión a internet"
    if isinstance(e, requests.exceptions.Timeout):
        return f"[{name}] Timeout — servidor lento, intenta de nuevo"
    if isinstance(e, requests.exceptions.HTTPError):
        code = e.response.status_code if e.response is not None else "?"
        label = _HTTP_LABELS.get(code, f"HTTP {code}")
        return f"[{name}] {label}"
    if isinstance(e, (json.JSONDecodeError, KeyError)):
        return f"[{name}] Respuesta inesperada del modelo"
    return f"[{name}] {type(e).__name__}: {e}"

# ── Interpreta con IA ──────────────────────────────────────────────────────────

def ask_ai(text: str) -> dict | None:
    """
    Prueba todos los proveedores en orden.
    Devuelve dict de acción o None si todos fallan.
    """
    global active_provider

    for p in PROVIDERS:
        key = p.get("api_key", "")
        if not key or "XXXX" in key:
            continue
        try:
            raw    = _call_gemini(p, text) if p["format"] == "gemini" else _call_openai(p, text)
            result = _parse(raw)
            active_provider = p["name"]
            return result
        except Exception as e:
            yield_msg = _describe_error(e, p["name"])
            # Usamos print aquí — el caller puede redirigir si quiere
            print(f"\033[2m  {yield_msg}\033[0m")

    active_provider = None
    return None

# ── Fuzzy matching sin modelo ──────────────────────────────────────────────────

def ask_fuzzy(text: str) -> dict:
    """Interpreta sin IA usando coincidencia de texto."""
    txt   = text.lower().strip()
    names = list(get_all().keys())

    # 1. Nombre exacto dentro del texto
    for name in names:
        if name in txt:
            return {"action": "open", "item": name}

    # 2. Fuzzy sobre texto completo
    m = difflib.get_close_matches(txt, names, n=1, cutoff=0.45)
    if m:
        score = difflib.SequenceMatcher(None, txt, m[0]).ratio()
        if score > 0.75:
            return {"action": "open", "item": m[0]}
        return {"action": "suggest", "item": m[0],
                "msg": f"¿Quisiste decir '{m[0]}'? (modo sin IA)"}

    # 3. Fuzzy palabra por palabra
    for word in txt.split():
        if len(word) < 3:
            continue
        m2 = difflib.get_close_matches(word, names, n=1, cutoff=0.6)
        if m2:
            return {"action": "suggest", "item": m2[0],
                    "msg": f"¿Quisiste decir '{m2[0]}'?"}

    return {"action": "not_found",
            "msg": f"No encontré '{text}'.\n  Disponibles: {', '.join(names)}"}

# ── API pública ────────────────────────────────────────────────────────────────

def interpret(text: str, force_fuzzy: bool = False) -> tuple[dict, bool]:
    """
    Interpreta el texto del usuario.
    Devuelve (accion_dict, usó_ia: bool).
    Si la IA falla y FALLBACK_NO_MODEL=True, usa fuzzy.
    """
    if not force_fuzzy:
        result = ask_ai(text)
        if result is not None:
            return result, True
        if not FALLBACK_NO_MODEL:
            return {"action": "error", "msg": "Sin IA disponible. Configura un proveedor en config.py"}, False

    return ask_fuzzy(text), False

def test_providers() -> list[dict]:
    """Prueba todos los proveedores. Devuelve lista de {name, ok, error}."""
    results = []
    for p in PROVIDERS:
        key = p.get("api_key", "")
        if not key or "XXXX" in key:
            results.append({"name": p["name"], "ok": None, "error": "No configurado"})
            continue
        try:
            _call_gemini(p, "hola") if p["format"] == "gemini" else _call_openai(p, "hola")
            results.append({"name": p["name"], "ok": True, "error": None})
        except Exception as e:
            results.append({"name": p["name"], "ok": False, "error": _describe_error(e, p["name"])})
    return results
