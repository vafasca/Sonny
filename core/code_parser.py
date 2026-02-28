"""
core/code_parser.py — Parser genérico de bloques de código.

PROBLEMA QUE RESUELVE:
  ChatGPT (y otros modelos) a veces devuelven código con \\n como literales
  en vez de saltos de línea reales. Esto hace que todo el contenido quede
  en una sola línea, rompiendo la escritura de archivos y el parseo de pasos.

SOLUCIÓN:
  1. normalize_newlines()  → convierte \\n, \\t, \\r literales a caracteres reales.
  2. extract_code_blocks() → regex genérico que captura CUALQUIER bloque ```lang``
     sin depender de listas fijas de lenguajes.
  3. blocks_to_files()     → mapea bloques a nombres de archivo automáticamente.

VENTAJAS:
  · Escalable: cualquier ``` ``` nuevo funciona sin tocar el código.
  · Multi-bloque: separa HTML + CSS + JS de una sola respuesta.
  · Robusto: funciona con o sin etiqueta de lenguaje.
  · Automático: extension mapping desde el nombre del lenguaje.
"""

import re
from pathlib import Path

# ── Mapa de lenguajes → extensiones ───────────────────────────────────────────
EXT_MAP: dict[str, str] = {
    # Web
    "html": "html", "htm": "html",
    "css": "css", "scss": "scss", "sass": "scss", "less": "css",
    "js": "js", "javascript": "js", "mjs": "js", "cjs": "js",
    "ts": "ts", "typescript": "ts",
    "jsx": "jsx", "tsx": "tsx",
    # Backend
    "py": "py", "python": "py",
    "java": "java",
    "kt": "kt", "kotlin": "kt",
    "go": "go",
    "rs": "rs", "rust": "rs",
    "rb": "rb", "ruby": "rb",
    "php": "php",
    "cs": "cs", "csharp": "cs", "c#": "cs",
    "cpp": "cpp", "c++": "cpp",
    "c": "c",
    "swift": "swift",
    # Config / data
    "json": "json",
    "yaml": "yaml", "yml": "yaml",
    "toml": "toml",
    "xml": "xml",
    "sql": "sql",
    "sh": "sh", "bash": "sh", "shell": "sh", "zsh": "sh",
    "ps1": "ps1", "powershell": "ps1",
    "bat": "bat", "cmd": "bat",
    "dockerfile": "dockerfile", "docker": "dockerfile",
    "makefile": "makefile",
    # Docs
    "md": "md", "markdown": "md",
    "txt": "txt", "text": "txt", "plaintext": "txt",
    # Otros
    "r": "r",
    "scala": "scala",
    "dart": "dart",
    "lua": "lua",
    "pl": "pl", "perl": "pl",
    "ex": "ex", "elixir": "ex",
    "hs": "hs", "haskell": "hs",
    "graphql": "graphql", "gql": "graphql",
    "proto": "proto",
}

# Lenguajes que NO deben usarse como extensión (son alias o meta-nombres)
_SKIP_LANG_LABELS = {
    "código", "codigo", "code", "output", "console", "terminal",
    "result", "resultado", "example", "ejemplo", "none", "text",
    "plain", "plaintext", "no language",
}


def normalize_newlines(text: str) -> str:
    """
    Normaliza saltos de línea escapados que ChatGPT y otros modelos a veces devuelven.

    Casos que maneja:
      · "line1\\nline2"   → "line1\nline2"   (literal \\n en JSON string)
      · "col1\\tcol2"     → "col1\tcol2"     (literal \\t)
      · "line1\\r\\nline2" → "line1\nline2"  (literal CRLF)

    NO toca textos que ya tienen saltos de línea reales.
    """
    if not text:
        return text

    # Detectar si hay \\n literales (doble backslash + n como caracteres reales)
    # Esto ocurre cuando el modelo responde dentro de un JSON y escapa los \\n
    has_real_newlines = '\n' in text
    has_literal_escaped = '\\n' in text  # backslash + n como dos caracteres

    if has_literal_escaped and not has_real_newlines:
        # Todo el texto está en "una sola línea" con \\n como separadores → expandir
        text = text.replace('\\r\\n', '\n').replace('\\r', '\n').replace('\\n', '\n')
        text = text.replace('\\t', '\t')
        # Limpiar dobles backslash que quedan de JSON encoding
        text = text.replace('\\\\', '\\')
        return text

    if has_literal_escaped and has_real_newlines:
        # Mezcla: hay algunos \\n literales entre líneas reales → reemplazar solo los literales
        # Usamos un marcador temporal para no tocar los \\n que son parte de regex/strings reales
        text = re.sub(r'(?<![\\])\\n', '\n', text)
        text = re.sub(r'(?<![\\])\\t', '\t', text)
        text = re.sub(r'(?<![\\])\\r', '', text)

    return text


def extract_code_blocks(text: str) -> list[dict]:
    """
    Extrae TODOS los bloques de código de una respuesta usando regex genérico.

    Patrón: ```[lang]\\ncontent```
    Captura cualquier lenguaje (o ninguno) sin lista fija.

    Returns:
        Lista de dicts con:
          - lang    : nombre del lenguaje (str, puede ser '')
          - content : contenido del bloque con newlines reales
          - ext     : extensión de archivo recomendada
    """
    if not text:
        return []

    # Paso 1: normalizar el texto completo
    text = normalize_newlines(text)

    blocks = []

    # Regex genérico:
    #   ``` → apertura
    #   (\w[\w+#.-]*)? → nombre de lenguaje opcional (ej: python, c++, dockerfile)
    #   [ \t]* → espacios opcionales tras el nombre
    #   \n? → salto de línea opcional (a veces el modelo omite el \\n)
    #   ([\s\S]*?) → contenido (cualquier cosa, lazy)
    #   ``` → cierre
    pattern = re.compile(
        r'```([\w+#.-]*)[ \t]*\n?([\s\S]*?)```',
        re.MULTILINE
    )

    for match in pattern.finditer(text):
        raw_lang = (match.group(1) or "").strip().lower()
        content  = match.group(2) or ""

        # Normalizar el contenido también (por si el modelo escapó solo el interior)
        content = normalize_newlines(content)

        # Limpiar líneas vacías al inicio/fin
        content = content.strip('\n').rstrip()

        if not content:
            continue

        # Omitir si el "lenguaje" es solo una etiqueta decorativa
        if raw_lang in _SKIP_LANG_LABELS:
            raw_lang = ""

        # Resolver extensión
        ext = EXT_MAP.get(raw_lang, raw_lang if raw_lang else "txt")

        blocks.append({
            "lang":    raw_lang,
            "content": content,
            "ext":     ext,
            "raw":     match.group(0),  # bloque completo original
        })

    return blocks


def blocks_to_files(
    blocks: list[dict],
    base_name: str = "output",
    single_file_priority: list[str] | None = None
) -> list[dict]:
    """
    Convierte bloques extraídos en descriptores de archivos listos para escribir.

    Args:
        blocks            : salida de extract_code_blocks()
        base_name         : nombre base sin extensión (ej: "app", "index")
        single_file_priority : si solo hay un bloque y su ext está aquí,
                              usa exactamente ese nombre (ej: ["html","py","js"])

    Returns:
        Lista de dicts: [{path, content, lang, ext}]
    """
    if not blocks:
        return []

    files = []
    ext_counts: dict[str, int] = {}

    # Si hay un solo bloque y tiene prioridad → nombre directo
    if len(blocks) == 1 and single_file_priority:
        b = blocks[0]
        if b["ext"] in single_file_priority:
            return [{
                "path":    f"{base_name}.{b['ext']}",
                "content": b["content"],
                "lang":    b["lang"],
                "ext":     b["ext"],
            }]

    for block in blocks:
        ext = block["ext"]
        ext_counts[ext] = ext_counts.get(ext, 0) + 1
        count = ext_counts[ext]

        # Primer archivo de cada tipo: base_name.ext
        # Archivos adicionales del mismo tipo: base_name_2.ext, etc.
        filename = f"{base_name}.{ext}" if count == 1 else f"{base_name}_{count}.{ext}"

        files.append({
            "path":    filename,
            "content": block["content"],
            "lang":    block["lang"],
            "ext":     ext,
        })

    return files


def fix_content_newlines(content: str) -> str:
    """
    Aplica normalize_newlines + limpieza básica a contenido de archivo.
    Útil para limpiar el campo 'content' que viene del JSON de la IA.
    """
    if not content:
        return content
    content = normalize_newlines(content)
    # Eliminar \r residuales
    content = content.replace('\r\n', '\n').replace('\r', '\n')
    # Asegurar que termina con exactamente un salto de línea
    content = content.rstrip('\n') + '\n'
    return content


def extract_first_block(text: str) -> str | None:
    """
    Extrae el contenido del primer bloque ``` que aparezca.
    Útil como fallback rápido cuando solo se espera un archivo.
    Returns None si no hay bloque.
    """
    blocks = extract_code_blocks(text)
    return blocks[0]["content"] if blocks else None


def parse_response_to_files(
    response: str,
    base_name: str = "output",
    fallback_ext: str = "py"
) -> list[dict]:
    """
    Pipeline completo: respuesta cruda → lista de archivos listos para escribir.

    Si no hay bloques ``` → intenta usar toda la respuesta como código.
    """
    blocks = extract_code_blocks(response)

    if blocks:
        return blocks_to_files(blocks, base_name)

    # Fallback: toda la respuesta como código plano
    content = fix_content_newlines(response)
    if content.strip():
        return [{
            "path":    f"{base_name}.{fallback_ext}",
            "content": content,
            "lang":    "",
            "ext":     fallback_ext,
        }]

    return []