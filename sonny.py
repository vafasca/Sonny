"""
sonny.py — Punto de entrada principal de Sonny.
"""
import os, sys

# Fix Git Bash: evita que el buffer se imprima múltiples veces
sys.stdout.reconfigure(line_buffering=True)
sys.stderr.reconfigure(line_buffering=True)

from core.ai           import interpret, test_providers, active_provider
from core.agent        import run_agent, es_tarea_agente
from core.orchestrator import run_orchestrator_with_site, detectar_navegadores, detect_angular_cli_version
from core.loop_guard import LoopGuardError
from core.validator import ValidationError
from core.planner import PlannerError
from core.ai_scraper   import shutdown_ai_scraper_runtime
from core.launcher     import launch
from core.registry     import get_all, item_type
from config            import PROVIDERS

class C:
    CYAN="\033[96m"; GREEN="\033[92m"; YELLOW="\033[93m"; RED="\033[91m"
    BOLD="\033[1m";  DIM="\033[2m";    RESET="\033[0m";   MAGENTA="\033[95m"

_SI = {"s","si","sí","yes","y","ok","dale","claro","correcto","exacto",
       "abrelo","ábrelo","abrir","abre","anda","venga","hazlo","adelante",
       "confirmo","confirmar","afirmativo","obvio","porfa","porfavor",
       "please","lanza","ejecuta","va","vamos"}
_NO = {"no","nope","nel","negativo","cancela","cancelar","olvida",
       "olvídalo","para","detente","stop"}

def es_si(texto):
    words = set(texto.lower().split())
    if words & _NO: return False
    return bool(words & _SI)

def cmd_lista():
    apps = get_all()
    print(f"\n{C.BOLD}  {'NOMBRE':<24} {'TIPO':<9} RUTA{C.RESET}")
    print(f"  {'─'*65}")
    for n, p in sorted(apps.items()):
        print(f"  {C.GREEN}{n:<24}{C.RESET} {C.DIM}[{item_type(p):<7}]{C.RESET}  {C.DIM}{p}{C.RESET}")
    print(f"\n  Total: {len(apps)} items\n")

def cmd_debug():
    print(f"\n{C.BOLD}  Testeando proveedores de IA...{C.RESET}")
    results = test_providers()
    for r in results:
        if r["ok"] is None:
            print(f"  {C.DIM}[{r['name']}] {r['error']}{C.RESET}")
        elif r["ok"]:
            print(f"  {C.GREEN}✅ [{r['name']}] OK{C.RESET}")
        else:
            print(f"  {C.RED}❌ [{r['name']}] {r['error']}{C.RESET}")
    print()
    return any(r["ok"] for r in results)

def cmd_ayuda():
    print(f"""
{C.BOLD}  Comandos especiales:{C.RESET}
  {C.CYAN}lista{C.RESET}    → ver todos los items
  {C.CYAN}debug{C.RESET}    → testear IA
  {C.CYAN}ayuda{C.RESET}    → esta ayuda
  {C.CYAN}salir{C.RESET}    → cerrar Sonny

{C.BOLD}  Modo Orquestador 🌐 (IAs web):{C.RESET}
  {C.DIM}"desarrolla una app en Angular"
  "crea un proyecto en React"
  "haz un backend con Django"{C.RESET}
    """)

def banner():
    keys_ok = sum(1 for p in PROVIDERS if p.get("api_key") and "XXXX" not in p["api_key"])
    ai_st = (f"{C.GREEN}{keys_ok} proveedor(es) configurado(s){C.RESET}"
             if keys_ok else f"{C.YELLOW}sin proveedores — modo fuzzy activo{C.RESET}")
    print(f"""
{C.CYAN}{C.BOLD}  ╔══════════════════════════════════╗
  ║         SONNY  v4.0            ║
  ╚══════════════════════════════════╝{C.RESET}
  {C.DIM}IA      : {ai_st}
  Items   : {len(get_all())} cargados
  Agente  : {C.GREEN if keys_ok else C.YELLOW}{'✅ disponible' if keys_ok else '⚠️  requiere IA'}{C.DIM}{C.RESET}
  Orquestador: {C.GREEN}✅ modo web activo{C.RESET}
  {C.YELLOW}Habla con Sonny — abre apps o programa con frameworks{C.RESET}
  {C.DIM}Escribe 'ayuda' para ver todos los comandos.{C.RESET}
    """)

CMDS_SALIR = {"salir","exit","quit","chau","bye"}


AI_OPTIONS = ["chatgpt", "claude", "gemini", "qwen"]


def _ask_preferred_ai(last_choice: str | None = None) -> str | None:
    default_choice = last_choice if last_choice in AI_OPTIONS else "claude"
    print(f"  {C.DIM}Elige IA web: chatgpt | claude | gemini | qwen (Enter={default_choice}){C.RESET}")
    raw = input(f"{C.CYAN}tú > {C.RESET}").strip().lower()
    if not raw:
        return default_choice

    normalized = raw.replace(" ", "")
    aliases = {
        "chatgpt": "chatgpt",
        "chatgpt.": "chatgpt",
        "chatgpt,": "chatgpt",
        "chatgpt!": "chatgpt",
        "gpt": "chatgpt",
        "chatgpt4": "chatgpt",
        "chatgpt-4": "chatgpt",
        "claude": "claude",
        "gemini": "gemini",
        "qwen": "qwen",
    }
    selected = aliases.get(normalized)
    if selected in AI_OPTIONS:
        return selected

    print(f"  {C.YELLOW}⚠️ IA no reconocida. Usando {default_choice}.{C.RESET}")
    return default_choice




def _extract_angular_version_hint(user_input: str) -> str | None:
    import re
    m = re.search(r"angular\s*(?:v|version)?\s*(\d+(?:\.\d+){0,2})", (user_input or "").lower())
    return m.group(1) if m else None


def _ask_angular_version_if_needed(user_input: str) -> str:
    hinted = _extract_angular_version_hint(user_input)
    if hinted:
        return hinted

    detected = detect_angular_cli_version()
    if detected not in {"not_installed", "unknown"}:
        print(f"  {C.DIM}Angular CLI detectado: {detected}{C.RESET}")
        return detected

    print(f"  {C.YELLOW}⚠️ No detecté Angular CLI instalado automáticamente.{C.RESET}")
    while True:
        raw = input(f"{C.CYAN}tú > Especifica versión Angular objetivo (ej: 17.3.8): {C.RESET}").strip()
        if raw:
            return raw

def _extract_preferred_ai(user_input: str) -> str | None:
    low = (user_input or "").lower()
    if "chatgpt" in low or "chat gpt" in low or "gpt" in low:
        return "chatgpt"
    if "claude" in low:
        return "claude"
    if "gemini" in low:
        return "gemini"
    if "qwen" in low:
        return "qwen"
    return None



def _run_web_orchestrator(user_input: str, preferred_ai_memory: str | None) -> str | None:
    preferred_ai = _extract_preferred_ai(user_input)
    if not preferred_ai:
        preferred_ai = _ask_preferred_ai(preferred_ai_memory)
    preferred_ai_memory = preferred_ai
    print(f"  {C.DIM}IA seleccionada: {preferred_ai}{C.RESET}")

    angular_hint = _ask_angular_version_if_needed(user_input)
    print(f"  {C.DIM}Angular objetivo: {angular_hint}{C.RESET}")

    try:
        run_orchestrator_with_site(user_input, preferred_site=preferred_ai, angular_cli_version_hint=angular_hint)
    except LoopGuardError as exc:
        print(f"  {C.RED}❌ Ejecución detenida por loop_guard:{C.RESET} {exc}")
        print(f"  {C.DIM}Tip: pide menos archivos por fase o más comandos de build/test entre escrituras.{C.RESET}")
    except ValidationError as exc:
        print(f"  {C.RED}❌ Validación bloqueó el plan/acciones:{C.RESET} {exc}")
    except PlannerError as exc:
        print(f"  {C.RED}❌ Planner no pudo obtener JSON válido:{C.RESET} {exc}")
    except Exception as exc:
        print(f"  {C.RED}❌ Error en orquestador:{C.RESET} {exc}")

    return preferred_ai_memory

def main():
    banner()
    pendiente  = None
    preferred_ai_memory: str | None = None
    modo_fuzzy = not any(p.get("api_key") and "XXXX" not in p["api_key"] for p in PROVIDERS)

    while True:
        try:
            tag        = f"{C.YELLOW}[sin IA]{C.RESET} " if modo_fuzzy else ""
            user_input = input(f"{tag}{C.CYAN}{C.BOLD}tú > {C.RESET}").strip()
            if not user_input: continue
            low = user_input.lower()

            if low in CMDS_SALIR:
                shutdown_ai_scraper_runtime()
                print(f"{C.CYAN}👋 Hasta luego.{C.RESET}"); break
            if low == "lista":   cmd_lista();  continue
            if low == "ayuda":   cmd_ayuda();  continue
            if low == "debug":
                ai_ok = cmd_debug()
                if ai_ok and modo_fuzzy:
                    modo_fuzzy = False
                    print(f"  {C.GREEN}✅ Modo IA restaurado.{C.RESET}\n")
                continue

            if pendiente:
                if es_si(low):
                    ok, msg = launch(pendiente)
                    print(f"{C.GREEN if ok else C.RED}{'✅' if ok else '❌'} {msg}{C.RESET}")
                else:
                    print(f"{C.DIM}  Cancelado.{C.RESET}")
                pendiente = None; continue

            FRAMEWORKS = ["angular","react","vue","next.js","nextjs","nuxt",
                          "svelte","flutter","django","rails","laravel",
                          "spring boot","springboot","express","fastapi"]
            TRIGGERS_WEB = ["usando web","busca en ia","pregunta a ","consulta a ",
                            "usa claude","usa chatgpt","usa gemini","usa qwen",
                            "navega y","web haz","pide a la ia"]

            needs_framework = any(f in low for f in FRAMEWORKS)
            is_web_task     = any(t in low for t in TRIGGERS_WEB)

            if (is_web_task or needs_framework) and not modo_fuzzy:
                if needs_framework and not is_web_task:
                    print(f"  {C.DIM}Framework detectado — usando orquestador web{C.RESET}")
                preferred_ai_memory = _run_web_orchestrator(user_input, preferred_ai_memory)
                continue

            if es_tarea_agente(user_input) and not modo_fuzzy:
                print(f"  {C.DIM}Tarea de desarrollo detectada — usando orquestador web{C.RESET}")
                preferred_ai_memory = _run_web_orchestrator(user_input, preferred_ai_memory)
                continue

            if not modo_fuzzy:
                print(f"{C.DIM}  [interpretando...]{C.RESET}", end="\r")

            accion, used_ai = interpret(user_input, force_fuzzy=modo_fuzzy)
            print(" " * 30, end="\r")

            if not used_ai and not modo_fuzzy:
                modo_fuzzy = True
                print(f"{C.YELLOW}⚠️  IA no disponible → modo fuzzy.\n   Escribe 'debug' para diagnosticar.{C.RESET}\n")

            action = accion.get("action")
            if action == "open":
                ok, msg = launch(accion.get("item",""))
                print(f"{C.GREEN if ok else C.RED}{'✅' if ok else '❌'} {msg}{C.RESET}")
            elif action == "suggest":
                item = accion.get("item","")
                msg  = accion.get("msg", f"¿Quisiste decir '{item}'?")
                print(f"{C.YELLOW}🤖 {msg} {C.DIM}(s/n){C.RESET}")
                pendiente = item
            elif action in ("not_found","unknown","help","error"):
                if not modo_fuzzy and any(t in low for t in ["crea","haz","escribe","genera","construye"]):
                    print(f"{C.YELLOW}🤖 No encontré esa app. ¿Programarlo? {C.DIM}(s/n){C.RESET}")
                    resp = input(f"{C.CYAN}tú > {C.RESET}").strip()
                    if es_si(resp.lower()): run_agent(user_input)
                else:
                    print(f"{C.YELLOW}🤖 {accion.get('msg','')}{C.RESET}")

        except KeyboardInterrupt:
            shutdown_ai_scraper_runtime()
            print(f"\n{C.CYAN}👋 Hasta luego.{C.RESET}"); break
if __name__ == "__main__":
    main()
