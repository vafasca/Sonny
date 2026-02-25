"""
core/agent.py — Agente autónomo de Sonny.
Recibe un objetivo, escribe código, lo ejecuta, lee errores y corrige solo.
Soporta: Python, JavaScript/Node, HTML+CSS, y más.
"""
import json, os, subprocess, sys, tempfile, re, shutil
from pathlib import Path
from datetime import datetime
from config   import PROVIDERS
from core.ai  import _call_openai, _call_gemini, _describe_error

# ── Configuración ──────────────────────────────────────────────────────────────
MAX_ITERATIONS  = 8       # máximo de intentos antes de rendirse
TIMEOUT_RUN     = 30      # segundos máximos para ejecutar código
WORKSPACE_ROOT  = Path(__file__).parent.parent / "workspace"

# ── Colores ────────────────────────────────────────────────────────────────────
class C:
    CYAN   = "\033[96m";  GREEN  = "\033[92m";  YELLOW = "\033[93m"
    RED    = "\033[91m";  BOLD   = "\033[1m";   DIM    = "\033[2m"
    BLUE   = "\033[94m";  RESET  = "\033[0m";   MAGENTA= "\033[95m"

# ── System prompt del agente ───────────────────────────────────────────────────
AGENT_SYSTEM = """Eres un agente de programación autónomo. Tu trabajo es recibir un objetivo,
escribir el código necesario, ejecutarlo, leer los resultados y corregir errores hasta que funcione.

SIEMPRE responde con un JSON válido y NADA MÁS. Sin texto extra, sin markdown, sin explicaciones fuera del JSON.

Acciones disponibles:

1. Escribir un archivo:
{"action":"write_file","path":"nombre.py","content":"...código aquí...","lang":"python"}

2. Ejecutar un comando:
{"action":"run","cmd":"python nombre.py","description":"Ejecutando el programa"}

3. Corregir un archivo existente (cuando hay error):
{"action":"fix_file","path":"nombre.py","content":"...código corregido...","error_fixed":"descripción del error que se corrigió"}

4. Tarea completada exitosamente:
{"action":"done","msg":"Descripción de lo que se logró","files":["lista","de","archivos","creados"]}

5. La tarea necesita input del usuario:
{"action":"ask","msg":"Pregunta específica al usuario"}

6. Tarea imposible con las herramientas disponibles:
{"action":"impossible","msg":"Explicación honesta de por qué no se puede"}

REGLAS CRÍTICAS:
- Escribe código COMPLETO y funcional, nunca fragmentos.
- Si hay un error de ejecución, analízalo y corrígelo en el siguiente paso.
- Para Python: usa print() para mostrar resultados.
- Para JavaScript: usa console.log() para mostrar resultados.
- Para HTML: crea archivos auto-contenidos (CSS y JS inline si es posible).
- Rutas de archivos: usa solo nombres simples (suma.py, no C:/carpeta/suma.py).
- Máximo 3 archivos por tarea. Prefiere soluciones en un solo archivo.
- Si el código requiere librerías externas, instálalas con pip/npm primero.

PROHIBIDO ABSOLUTAMENTE:
- NUNCA uses input() en Python — el código corre sin terminal interactiva.
- NUNCA intentes crear proyectos Angular, React, Vue, Flutter, Django, Rails o cualquier
  framework que requiera CLI propio. Esos los maneja otro módulo. Si el usuario pide eso,
  responde: {"action":"impossible","msg":"Este framework requiere CLI — usa el modo orquestador web."}
- NUNCA uses readline(), prompt(), scanner, o cualquier lectura de stdin.
- En su lugar: usa valores de ejemplo hardcodeados para demostrar la funcionalidad.
- Ejemplo correcto: num1, num2 = 5, 3  →  print(f"{num1} + {num2} = {num1+num2}")
- Para apps interactivas (calculadora, formulario): créalas en HTML con JavaScript.

REGLAS ESPECÍFICAS POR LENGUAJE:
- JAVA: El nombre del archivo DEBE coincidir EXACTAMENTE con el nombre de la clase pública.
  Si la clase se llama "Suma", el archivo DEBE llamarse "Suma.java" (con S mayúscula).
  Comando de compilación: javac Suma.java
  Comando de ejecución:   java Suma
  NUNCA uses Scanner o BufferedReader — usa valores hardcodeados.
- PYTHON: archivo.py → python archivo.py
- JAVASCRIPT PARA BROWSER (HTML+JS): SIEMPRE pon el JS inline dentro del HTML con <script>.
  NUNCA crees un archivo .js separado cuando la tarea es una web/formulario/UI.
  Los archivos .js separados son solo para Node.js puro (sin HTML).
- HTML: crea SIEMPRE un único archivo .html con CSS en <style> y JS en <script> adentro.
  El archivo se abrirá automáticamente en el navegador al terminar.
"""

# ── Detección de tipo de tarea ─────────────────────────────────────────────────

TRIGGERS_AGENTE = [
    "desarrolla", "crea", "construye", "programa", "escribe",
    "haz una app", "haz un", "genera", "implementa", "codea",
    "make", "build", "create", "develop", "write a", "código",
    "aplicación", "script", "programa que", "función que",
]

def es_tarea_agente(texto: str) -> bool:
    """Detecta si el texto es una tarea de desarrollo, no solo abrir una app."""
    low = texto.lower()
    return any(t in low for t in TRIGGERS_AGENTE)

# ── Llamada a IA con historial ─────────────────────────────────────────────────

def _call_agent_ai(messages: list[dict]) -> dict | None:
    """Llama al mejor proveedor disponible con historial de conversación."""
    for p in PROVIDERS:
        key = p.get("api_key", "")
        if not key or "XXXX" in key:
            continue
        try:
            if p["format"] == "gemini":
                # Gemini: concatenar todo en un texto
                full = AGENT_SYSTEM + "\n\n"
                for m in messages:
                    role = "Usuario" if m["role"] == "user" else "Agente"
                    full += f"{role}: {m['content']}\n\n"
                raw = _call_gemini(p, full)
            else:
                # OpenAI-compatible: messages con system prompt
                import requests
                headers = {
                    "Authorization": f"Bearer {p['api_key']}",
                    "Content-Type":  "application/json",
                    **(p.get("extra_headers") or {}),
                }
                payload = {
                    "model":    p["model"],
                    "messages": [{"role":"system","content":AGENT_SYSTEM}] + messages,
                    "temperature": 0.2,
                    "max_tokens":  2000,
                }
                r = requests.post(p["url"], headers=headers, json=payload, timeout=30)
                r.raise_for_status()
                raw = r.json()["choices"][0]["message"]["content"].strip()

            # Limpiar y parsear JSON
            clean = raw.replace("```json","").replace("```","").strip()
            # Extraer primer objeto JSON si hay texto extra
            match = re.search(r'\{.*\}', clean, re.DOTALL)
            if match:
                return json.loads(match.group())
            return json.loads(clean)

        except Exception as e:
            print(f"{C.DIM}  {_describe_error(e, p['name'])}{C.RESET}")

    return None

# ── Ejecutor de comandos ───────────────────────────────────────────────────────

def _run_command(cmd: str, cwd: Path) -> tuple[bool, str]:
    """
    Ejecuta un comando en el workspace.
    Devuelve (éxito, output_completo).
    """
    try:
        result = subprocess.run(
            cmd, shell=True, cwd=str(cwd),
            capture_output=True, text=True,
            timeout=TIMEOUT_RUN,
            stdin=subprocess.DEVNULL,    # nunca espera input del usuario
            encoding="utf-8", errors="replace"
        )
        output = ""
        if result.stdout.strip():
            output += result.stdout.strip()
        if result.stderr.strip():
            output += ("\n" if output else "") + "[STDERR]\n" + result.stderr.strip()

        success = result.returncode == 0
        return success, output or "(sin output)"

    except subprocess.TimeoutExpired:
        return False, f"[TIMEOUT] El programa tardó más de {TIMEOUT_RUN}s y fue detenido."
    except Exception as e:
        return False, f"[ERROR INTERNO] {e}"

def _detect_runner(path: str) -> str:
    """Devuelve el comando para ejecutar un archivo según su extensión."""
    ext = Path(path).suffix.lower()
    runners = {
        ".py":   f"python {path}",
        ".js":   f"node {path}",
        ".ts":   f"npx ts-node {path}",
        ".sh":   f"bash {path}",
        # .html no se ejecuta aquí — lo abre _demo_visual al final
    }
    return runners.get(ext, f"python {path}")

# ── Workspace ──────────────────────────────────────────────────────────────────

def _create_workspace(nombre: str) -> Path:
    """Crea una carpeta limpia para la tarea."""
    safe = re.sub(r'[^\w\-]', '_', nombre.lower())[:30]
    ts   = datetime.now().strftime("%H%M%S")
    ws   = WORKSPACE_ROOT / f"{safe}_{ts}"
    ws.mkdir(parents=True, exist_ok=True)
    return ws

# ── Demo visual ────────────────────────────────────────────────────────────────

def _demo_visual(workspace: Path, archivos: list[str], _opened: list = []):
    """
    Abre el resultado UNA SOLA VEZ usando _opened como bandera mutable.
    - HTML → navegador automáticamente
    - Python/JS → pregunta si quiere terminal
    """
    # Bandera: si ya abrimos algo en esta sesión de tarea, salir
    if _opened:
        return
    if not archivos:
        return

    # Prioridad: HTML > Python > JS
    main_file = None
    for ext in (".html", ".py", ".js", ".ts"):
        for f in archivos:
            if f.lower().endswith(ext):
                main_file = f
                break
        if main_file:
            break

    if not main_file:
        # Buscar directamente en workspace por si la lista está incompleta
        for ext in (".html", ".py", ".js"):
            found = list(workspace.glob(f"*{ext}"))
            if found:
                main_file = found[0].name
                break

    if not main_file:
        return

    ext       = Path(main_file).suffix.lower()
    full_path = workspace / main_file

    if not full_path.exists():
        return

    try:
        if ext == ".html":
            print(f"  {C.CYAN}{'─'*44}{C.RESET}")
            print(f"  {C.GREEN}🌐 Abriendo en el navegador...{C.RESET}\n")
            os.startfile(str(full_path))
            _opened.append(True)   # marcar como abierto

        elif ext in (".py", ".js"):
            print(f"\n  {C.CYAN}{'─'*44}{C.RESET}")
            print(f"  {C.YELLOW}¿Quieres probar el programa en una terminal? {C.DIM}(s/n){C.RESET}")
            resp = input(f"  {C.CYAN}tú > {C.RESET}").strip().lower()
            if not resp or resp[0] not in ("s", "y"):
                return
            runner = "python" if ext == ".py" else "node"
            cmd = f'start cmd /k "cd /d {workspace} && {runner} {main_file} & echo. & pause"'
            subprocess.Popen(cmd, shell=True)
            print(f"  {C.GREEN}🖥️  Terminal abierta con {main_file}{C.RESET}\n")
            _opened.append(True)

    except Exception as e:
        print(f"  {C.RED}No pude abrir la demo: {e}{C.RESET}\n")


# ── Loop principal del agente ──────────────────────────────────────────────────

def run_agent(objetivo: str) -> bool:
    """
    Ejecuta el agente autónomo para cumplir el objetivo.
    Devuelve True si completó la tarea.
    """
    print(f"\n{C.MAGENTA}{C.BOLD}  🤖 MODO AGENTE ACTIVADO{C.RESET}")
    print(f"  {C.DIM}Objetivo: {objetivo}{C.RESET}")

    workspace = _create_workspace(objetivo[:30])
    print(f"  {C.DIM}Workspace: {workspace}{C.RESET}\n")

    messages: list[dict] = [
        {"role": "user", "content": f"Objetivo: {objetivo}\n\nEmpieza escribiendo el código necesario."}
    ]

    archivos_creados: list[str] = []
    last_run_output: str = ""
    last_action_hash: str = ""   # evita procesar la misma acción dos veces

    for i in range(1, MAX_ITERATIONS + 1):
        print(f"{C.BLUE}{C.BOLD}  ── Paso {i} ──{C.RESET}")
        print(f"  {C.DIM}Consultando IA...{C.RESET}")

        accion = _call_agent_ai(messages)

        if accion is None:
            print(f"{C.RED}  ❌ La IA no respondió. Sin proveedores disponibles.{C.RESET}")
            return False

        # Detectar respuesta duplicada
        import hashlib
        action_hash = hashlib.md5(json.dumps(accion, sort_keys=True).encode()).hexdigest()
        if action_hash == last_action_hash:
            print(f"  {C.DIM}   (respuesta duplicada ignorada){C.RESET}")
            # Pedir a la IA que avance
            messages.append({"role": "user", "content": "Continúa con el siguiente paso."})
            last_action_hash = ""
            continue
        last_action_hash = action_hash

        action_type = accion.get("action", "unknown")

        # ── write_file ─────────────────────────────────────────────────────
        if action_type in ("write_file", "fix_file"):
            path    = accion.get("path", "output.py")
            content = accion.get("content", "")
            lang    = accion.get("lang", Path(path).suffix.lstrip(".") or "python")
            fixed   = accion.get("error_fixed", "")

            # Para Java: si la IA cambia el nombre del archivo, limpiar el viejo
            if Path(path).suffix.lower() == ".java":
                # Eliminar cualquier .java previo que ya no sea válido
                for old_java in workspace.glob("*.java"):
                    if old_java.name != path:
                        old_java.unlink(missing_ok=True)
                # Eliminar .class compilados anteriores (pueden causar conflictos)
                for cls in workspace.glob("*.class"):
                    cls.unlink(missing_ok=True)

            # Si crean un .js separado pero ya existe un .html → fusionar inline
            if Path(path).suffix.lower() == ".js" and list(workspace.glob("*.html")):
                html_files = list(workspace.glob("*.html"))
                html_path  = html_files[0]
                html_src   = html_path.read_text(encoding="utf-8")
                # Limpiar etiquetas <script> del contenido JS si las trae
                js_clean = re.sub(r'</?script[^>]*>', '', content).strip()
                script_tag = f"\n<script>\n{js_clean}\n</script>\n"
                if "</body>" in html_src:
                    merged = html_src.replace("</body>", script_tag + "</body>")
                else:
                    merged = html_src + script_tag
                html_path.write_text(merged, encoding="utf-8")
                print(f"  {C.YELLOW}📎 JS fusionado en {html_path.name} (no se crea .js separado){C.RESET}")
                messages.append({"role": "assistant", "content": json.dumps(accion)})
                messages.append({"role": "user",
                    "content": f"El JS fue fusionado directamente en el HTML. No se creó archivo .js separado. ¿La tarea está completa? Responde con action:done."
                })
                continue

            full_path = workspace / path
            full_path.write_text(content, encoding="utf-8")
            if path not in archivos_creados:
                archivos_creados.append(path)

            if action_type == "fix_file":
                print(f"  {C.YELLOW}🔧 Corrigiendo: {path}{C.RESET}")
                print(f"  {C.DIM}   Error solucionado: {fixed}{C.RESET}")
            else:
                print(f"  {C.GREEN}📝 Archivo creado: {path} ({lang}){C.RESET}")

            # Auto-ejecutar si es código ejecutable
            ext_path = Path(path).suffix.lower()
            # Si hay HTML en el workspace, el JS es browser-only → no ejecutar con Node
            has_html = any(workspace.glob("*.html"))
            if ext_path in (".py", ".ts") or (ext_path == ".js" and not has_html):
                cmd = _detect_runner(path)
                print(f"  {C.DIM}   Ejecutando: {cmd}{C.RESET}")
                ok, output = _run_command(cmd, workspace)
                last_run_output = output
                _print_output(ok, output)

                # Alimentar resultado a la IA
                status = "ÉXITO" if ok else "ERROR"
                messages.append({"role": "assistant", "content": json.dumps(accion)})
                messages.append({"role": "user",
                    "content": f"Resultado de ejecutar {path}:\n[{status}]\n{output}\n\n"
                               + ("✅ Funciona. ¿Está la tarea completa? Si sí, responde con action:done."
                                  if ok else
                                  "❌ Hay errores. Analiza el error y corrige el código.")
                })
            elif ext_path == ".js" and has_html:
                # JS de browser incluido → solo confirmar
                print(f"  {C.DIM}   JS de browser (se ejecuta en el navegador, no en Node){C.RESET}")
                messages.append({"role": "assistant", "content": json.dumps(accion)})
                messages.append({"role": "user",
                    "content": f"Archivo {path} creado. Es JS para browser, se ejecutará junto al HTML. ¿Tarea completa? Responde con action:done."
                })
            elif ext_path == ".java":
                # Java: compilar primero, luego ejecutar
                class_name = Path(path).stem  # Suma.java → Suma
                compile_ok, compile_out = _run_command(f"javac {path}", workspace)
                if compile_ok:
                    print(f"  {C.DIM}   Compilado ✅ → ejecutando {class_name}{C.RESET}")
                    ok, output = _run_command(f"java {class_name}", workspace)
                    last_run_output = output
                    _print_output(ok, output)
                    status = "ÉXITO" if ok else "ERROR"
                else:
                    print(f"  {C.DIM}   Compilación fallida{C.RESET}")
                    output = compile_out
                    last_run_output = output
                    _print_output(False, output)
                    status = "ERROR DE COMPILACIÓN"
                messages.append({"role": "assistant", "content": json.dumps(accion)})
                messages.append({"role": "user",
                    "content": f"Resultado Java ({path}):\n[{status}]\n{output}\n\n"
                               + ("✅ Funciona. ¿Tarea completa? Responde con action:done."
                                  if status == "ÉXITO" else
                                  "❌ Error. Analiza y corrige. Recuerda: el nombre del archivo DEBE ser igual al nombre de la clase pública con mayúscula exacta.")
                })
            else:
                # HTML u otro archivo no ejecutable: notificar a la IA
                is_html = ext_path == ".html"
                messages.append({"role": "assistant", "content": json.dumps(accion)})
                messages.append({"role": "user",
                    "content": (
                        f"Archivo {path} creado. El HTML se abrirá en el navegador al finalizar. "
                        f"Si el trabajo está completo, responde con action:done."
                        if is_html else
                        f"Archivo {path} creado. ¿Qué sigue?"
                    )
                })

        # ── run ────────────────────────────────────────────────────────────
        elif action_type == "run":
            cmd  = accion.get("cmd", "")
            desc = accion.get("description", cmd)

            # Si el comando intenta abrir un HTML en el navegador, ignorarlo.
            # _demo_visual lo abrirá al final sin duplicar.
            cmd_low = cmd.lower().strip()
            is_browser_open = (
                any(cmd_low.startswith(x) for x in ("start ", "open ", "xdg-open "))
                and any(ext in cmd_low for ext in (".html", ".htm"))
            )
            if is_browser_open:
                print(f"  {C.DIM}   (apertura de HTML diferida al final){C.RESET}")
                messages.append({"role": "assistant", "content": json.dumps(accion)})
                messages.append({"role": "user",
                    "content": "El HTML se abrirá en el navegador al finalizar. ¿La tarea está completa? Responde con action:done."
                })
                continue

            print(f"  {C.CYAN}▶  {desc}{C.RESET}")
            ok, output = _run_command(cmd, workspace)
            last_run_output = output
            _print_output(ok, output)

            messages.append({"role": "assistant", "content": json.dumps(accion)})
            messages.append({"role": "user",
                "content": f"Resultado:\n[{'ÉXITO' if ok else 'ERROR'}]\n{output}\n\n"
                           + ("¿Tarea completa? Responde con action:done si sí."
                              if ok else
                              "Hay errores. Corrígelos.")
            })

        # ── done ───────────────────────────────────────────────────────────
        elif action_type == "done":
            msg    = accion.get("msg", "Tarea completada.")
            files  = accion.get("files", archivos_creados)
            print(f"\n{C.GREEN}{C.BOLD}  ✅ TAREA COMPLETADA{C.RESET}")
            print(f"  {msg}")
            if files:
                print(f"\n  {C.DIM}Archivos en: {workspace}{C.RESET}")
                for f in files:
                    fp = workspace / f
                    print(f"  {C.GREEN}  📄 {f}{C.RESET}{C.DIM} {'✅' if fp.exists() else '⚠️  no encontrado'}{C.RESET}")
            if last_run_output and last_run_output != "(sin output)":
                print(f"\n  {C.CYAN}Último output:{C.RESET}")
                for line in last_run_output.splitlines()[:10]:
                    print(f"  {C.DIM}  {line}{C.RESET}")
            print(f"\n  {C.DIM}Abre la carpeta: explorer \"{workspace}\"{C.RESET}")
            _demo_visual(workspace, archivos_creados)
            return True

        # ── ask ────────────────────────────────────────────────────────────
        elif action_type == "ask":
            msg = accion.get("msg", "¿Puedes darme más detalles?")
            print(f"\n  {C.YELLOW}🤖 {msg}{C.RESET}")
            respuesta = input(f"  {C.CYAN}tú > {C.RESET}").strip()
            messages.append({"role": "assistant", "content": json.dumps(accion)})
            messages.append({"role": "user", "content": respuesta})

        # ── impossible ─────────────────────────────────────────────────────
        elif action_type == "impossible":
            print(f"\n  {C.RED}⚠️  {accion.get('msg', 'No puedo completar esta tarea.')}{C.RESET}\n")
            return False

        else:
            print(f"  {C.DIM}Acción desconocida: {action_type}. Reintentando...{C.RESET}")
            messages.append({"role": "assistant", "content": json.dumps(accion)})
            messages.append({"role": "user", "content": "No entendí esa acción. Por favor usa solo las acciones permitidas."})

    print(f"\n{C.RED}  ⚠️  Máximo de iteraciones alcanzado ({MAX_ITERATIONS}).{C.RESET}")
    print(f"  {C.DIM}Archivos guardados en: {workspace}{C.RESET}\n")
    return False

# ── Helper de output ───────────────────────────────────────────────────────────

def _print_output(ok: bool, output: str):
    color  = C.GREEN if ok else C.RED
    icon   = "✅" if ok else "❌"
    lines  = output.splitlines()
    limite = 15

    print(f"  {color}{icon} Output:{C.RESET}")
    for line in lines[:limite]:
        print(f"  {C.DIM}   {line}{C.RESET}")
    if len(lines) > limite:
        print(f"  {C.DIM}   ... (+{len(lines)-limite} líneas más){C.RESET}")
    print()