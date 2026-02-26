# SONNY — Reglas de Arquitectura
> Leer este archivo antes de modificar cualquier módulo del orquestador.

---

## La Regla Fundamental

```
🧠 CEREBRO = IA Web (ChatGPT / Claude / Gemini en el NAVEGADOR)
💪 CUERPO  = Sonny (Python) + Groq/API local
```

**El cerebro piensa. El cuerpo ejecuta. Nunca al revés.**

---

## Qué hace cada parte

### 🧠 IA Web (cerebro)
- Decide qué herramientas se necesitan
- Genera el código y los archivos
- Define los pasos de desarrollo
- Resuelve errores y corrige problemas
- Valida compatibilidad de versiones
- **TODA decisión técnica viene de aquí**

### 💪 Sonny / Groq (cuerpo)
- Ejecuta comandos del sistema (`subprocess`)
- Crea y escribe archivos en disco
- Verifica versiones instaladas (`node --version`, etc.)
- Parsea la respuesta de la IA y extrae los pasos
- Muestra mensajes en la terminal
- **NUNCA genera soluciones ni código propio**

---

## Flujo correcto del orquestador

```
Usuario pide: "desarrollar app Angular Hola Mundo"
         │
         ▼
TURNO 1 → Abrir navegador → ChatGPT
  Prompt: "Necesito X. ¿Qué herramientas necesito?"
  ChatGPT responde: "- Node.js\n- npm\n- Angular CLI"
         │
         ▼
SONNY verifica (subprocess, sin IA):
  node --version  → 20.19.0 ✅
  npm --version   → 11.10.1 ✅
  ng version      → 21.1.5  ✅
         │
         ▼
TURNO 2 → Mismo navegador → ChatGPT
  Prompt: "Tengo Node 20.19.0, npm 11.10.1, Angular CLI 21.1.5.
           Dame los pasos para [objetivo]."
  ChatGPT responde: pasos completos con comandos y archivos
         │
         ▼
SONNY ejecuta (subprocess, sin IA):
  Step 1: ng new hola-mundo-app ...
  Step 2: ng generate component saludo
  Step 3: escribir src/app/saludo/saludo.component.html
  Step 4: escribir src/app/saludo/saludo.component.css
  Step 5: ng serve --open
         │
         ▼
  ✅ App corriendo en http://localhost:4200
```

---

## Reglas de código

### ✅ PERMITIDO en `orchestrator.py`
```python
subprocess.run(cmd, ...)          # ejecutar comandos
path.write_text(content, ...)     # escribir archivos
ask_ai_multiturn([prompt], ...)   # consultar IA web
_check_tools_from_list(resp)      # verificar versiones
```

### ⛔ PROHIBIDO en `orchestrator.py`
```python
requests.post(GROQ_URL, ...)      # ❌ No usar API de Groq aquí
_call_openai(provider, prompt)    # ❌ No usar providers aquí
from config import PROVIDERS      # ❌ No importar providers aquí
```

> **PROVIDERS (Groq, Gemini API, OpenRouter) son solo para `agent.py`**
> El agente ejecuta tareas simples de código. El orquestador usa el navegador.

---

## Qué NO debe hacer el orquestador

| ❌ Incorrecto | ✅ Correcto |
|---|---|
| Groq genera los pasos de Angular | ChatGPT en navegador genera los pasos |
| Sonny decide qué archivos crear | ChatGPT decide qué archivos crear |
| Sonny crea el proyecto sin preguntar | ChatGPT dice exactamente qué crear |
| Groq corrige los errores de compilación | ChatGPT recibe el error y da la corrección |

---

## Prompts que envía Sonny

### Turno 1 (¿qué instalar?)
```
Necesito [objetivo].

¿Qué herramientas necesito tener instaladas?
Responde ÚNICAMENTE con la lista. Sin comandos de instalación,
sin tutoriales, sin explicaciones. Solo los nombres.
```

### Turno 2 (dame los pasos)
```
Tengo instalado en mi sistema:
  - Node.js: 20.19.0
  - npm: 11.10.1
  - Angular CLI: 21.1.5

TAREA: [objetivo]

Dame los pasos exactos y completos para lograrlo, incluyendo:
  - Crear el proyecto desde cero
  - Todos los archivos a modificar con su contenido completo
  - El comando para ejecutar la aplicación al final

Solo pasos y comandos. Sin explicaciones teóricas.
```

### Turno de corrección (error)
```
Estoy creando: [objetivo]
Tengo instalado: [versiones]

Falló este paso:
  Descripción: [desc]
  Comando: [cmd]
  Error: [stderr]

Dame los pasos corregidos para solucionar este error.
```

---


## Login persistente en ChatGPT

- Sonny guarda cookies/sesión en `sessions/chatgpt_session`.
- Si inicias sesión una vez (manual o automático), debería persistir al cerrar/abrir Sonny.
- Para login automático opcional (sin escribir usuario/clave en cada corrida), define estas variables de entorno antes de ejecutar Sonny:

```bash
export CHATGPT_EMAIL="tu_correo"
export CHATGPT_PASSWORD="tu_password"
python sonny.py
```

En Windows PowerShell:
```powershell
$env:CHATGPT_EMAIL="tu_correo"
$env:CHATGPT_PASSWORD="tu_password"
python sonny.py
```

> Si tu cuenta tiene 2FA/Captcha, Sonny intentará autologin y luego te dejará terminar manualmente en el navegador.
> Si ChatGPT muestra **"Iniciar sesión"**, Sonny ahora lo tratará como modo invitado y forzará autenticación cuando detecte `CHATGPT_EMAIL`/`CHATGPT_PASSWORD`.

---

## Archivos del proyecto

```
sonny/
├── sonny.py              # entrada principal
├── config.py             # API keys (Groq, Gemini) — solo para agent.py
├── SONNY_RULES.md        # ← este archivo
├── core/
│   ├── agent.py          # agente para Python/JS/HTML (usa PROVIDERS)
│   ├── orchestrator.py   # orquestador web (usa navegador, NO PROVIDERS)
│   ├── ai_scraper.py     # scraper del navegador
│   ├── browser.py        # configuración de sitios de IA
│   ├── prereqs.py        # verificación de prerrequisitos
│   └── web_log.py        # log de interacciones
└── workspace/            # proyectos generados
```

---

## Por qué Groq no puede ser el cerebro

1. **No sabe lo que tienes instalado** — ChatGPT recibe las versiones reales
2. **No tiene contexto del proyecto real** — ChatGPT recibe el árbol de archivos
3. **Modelos free no siguen instrucciones** — ChatGPT Plus sí sigue el formato
4. **El usuario quiere ver la IA trabajar en el navegador** — experiencia visual

---

*Versión: v11.0 — Actualizar cuando cambie la arquitectura*