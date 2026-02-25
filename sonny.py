"""
sonny.py — Punto de entrada principal de Sonny.
"""
import os
from core.ai       import interpret, test_providers, active_provider
from core.launcher import launch
from core.registry import get_all, item_type
from config        import PROVIDERS

# ── Colores ────────────────────────────────────────────────────────────────────
class C:
    CYAN   = "\033[96m";  GREEN  = "\033[92m";  YELLOW = "\033[93m"
    RED    = "\033[91m";  BOLD   = "\033[1m";   DIM    = "\033[2m"
    RESET  = "\033[0m"

# ── Palabras afirmativas / negativas ───────────────────────────────────────────
_SI = {"s","si","sí","yes","y","ok","dale","claro","correcto","exacto",
       "abrelo","ábrelo","abrir","abre","anda","venga","hazlo","adelante",
       "confirmo","confirmar","afirmativo","obvio","porfa","porfavor",
       "please","lanza","ejecuta","va","vamos"}
_NO = {"no","nope","nel","negativo","cancela","cancelar","olvida",
       "olvídalo","para","detente","stop"}

def es_si(texto: str) -> bool:
    words = set(texto.lower().split())
    if words & _NO:
        return False
    return bool(words & _SI)

# ── Comandos internos ──────────────────────────────────────────────────────────

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
  {C.CYAN}lista{C.RESET}    → ver todos los items disponibles
  {C.CYAN}debug{C.RESET}    → testear conexión con proveedores de IA
  {C.CYAN}ayuda{C.RESET}    → mostrar esta ayuda
  {C.CYAN}salir{C.RESET}    → cerrar Sonny

{C.BOLD}  Ejemplos de uso:{C.RESET}
  {C.DIM}"abre chrome"           → abre Google Chrome
  "quiero grabar pantalla"  → abre OBS
  "lanza steam por favor"   → abre Steam
  "abre ods"                → sugiere obs{C.RESET}
    """)

# ── Banner ─────────────────────────────────────────────────────────────────────

def banner():
    keys_ok = sum(1 for p in PROVIDERS if p.get("api_key") and "XXXX" not in p["api_key"])
    if keys_ok:
        ai_st = f"{C.GREEN}{keys_ok} proveedor(es) configurado(s){C.RESET}"
    else:
        ai_st = f"{C.YELLOW}sin proveedores — modo fuzzy activo{C.RESET}"
    print(f"""
{C.CYAN}{C.BOLD}  ╔══════════════════════════════════╗
  ║         SONNY  v3.1            ║
  ╚══════════════════════════════════╝{C.RESET}
  {C.DIM}IA     : {ai_st}
  Items  : {len(get_all())} cargados{C.RESET}
  {C.YELLOW}Habla con Sonny en tus propias palabras.{C.RESET}
  {C.DIM}Escribe 'ayuda' para ver comandos especiales.{C.RESET}
    """)

# ── Loop principal ─────────────────────────────────────────────────────────────

CMDS_SALIR = {"salir", "exit", "quit", "chau", "bye"}

def main():
    banner()

    pendiente  : str | None = None   # nombre de app esperando confirmación
    modo_fuzzy : bool = not any(
        p.get("api_key") and "XXXX" not in p["api_key"] for p in PROVIDERS
    )

    while True:
        try:
            tag        = f"{C.YELLOW}[sin IA]{C.RESET} " if modo_fuzzy else ""
            user_input = input(f"{tag}{C.CYAN}{C.BOLD}tú > {C.RESET}").strip()

            if not user_input:
                continue

            low = user_input.lower()

            # ── Comandos internos ──────────────────────────────────────────
            if low in CMDS_SALIR:
                print(f"{C.CYAN}👋 Hasta luego.{C.RESET}")
                break

            if low == "lista":
                cmd_lista(); continue

            if low == "ayuda":
                cmd_ayuda(); continue

            if low == "debug":
                ai_ok = cmd_debug()
                if ai_ok and modo_fuzzy:
                    modo_fuzzy = False
                    print(f"  {C.GREEN}✅ Modo IA restaurado.{C.RESET}\n")
                continue

            # ── Confirmación pendiente ─────────────────────────────────────
            if pendiente:
                if es_si(low):
                    ok, msg = launch(pendiente)
                    print(f"{C.GREEN if ok else C.RED}{'✅' if ok else '❌'} {msg}{C.RESET}")
                else:
                    print(f"{C.DIM}  Cancelado.{C.RESET}")
                pendiente = None
                continue

            # ── Interpretar ────────────────────────────────────────────────
            if not modo_fuzzy:
                prv = active_provider or "IA"
                print(f"{C.DIM}  [interpretando con {prv}...]{C.RESET}", end="\r")

            accion, used_ai = interpret(user_input, force_fuzzy=modo_fuzzy)
            print(" " * 45, end="\r")

            # Si la IA falló y se usó fuzzy, activar modo fuzzy permanente
            if not used_ai and not modo_fuzzy:
                modo_fuzzy = True
                print(f"{C.YELLOW}⚠️  IA no disponible → modo sin modelo activado.")
                print(f"   Escribe 'debug' para diagnosticar o 'ayuda' para ver opciones.{C.RESET}\n")

            # ── Ejecutar acción ────────────────────────────────────────────
            action = accion.get("action")

            if action == "open":
                ok, msg = launch(accion.get("item", ""))
                print(f"{C.GREEN if ok else C.RED}{'✅' if ok else '❌'} {msg}{C.RESET}")

            elif action == "suggest":
                item = accion.get("item", "")
                msg  = accion.get("msg", f"¿Quisiste decir '{item}'?")
                print(f"{C.YELLOW}🤖 {msg} {C.DIM}(s/n){C.RESET}")
                pendiente = item

            elif action in ("not_found", "unknown", "help", "error"):
                print(f"{C.YELLOW}🤖 {accion.get('msg', '')}{C.RESET}")

        except KeyboardInterrupt:
            print(f"\n{C.CYAN}👋 Hasta luego.{C.RESET}")
            break

if __name__ == "__main__":
    main()
