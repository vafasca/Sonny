"""
sonny_diag.py — Extrae las secciones relevantes de los archivos reales
para que el parche se pueda aplicar correctamente.
Ejecutar desde la raíz de Sonny: python sonny_diag.py
"""
import re
from pathlib import Path

ROOT = Path(__file__).parent

def extract_section(path, start_pattern, lines_after=60):
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
        lines = text.splitlines()
        for i, line in enumerate(lines):
            if re.search(start_pattern, line):
                chunk = lines[i:i+lines_after]
                return "\n".join(chunk)
        return f"[NO ENCONTRADO: {start_pattern}]"
    except Exception as e:
        return f"[ERROR: {e}]"

print("="*70)
print("DIAG: core/orchestrator.py — _sanitize_content")
print("="*70)
print(extract_section(ROOT/"core"/"orchestrator.py", r"def _sanitize_content"))

print("\n" + "="*70)
print("DIAG: core/orchestrator.py — _parse_structured (loop cl)")
print("="*70)
print(extract_section(ROOT/"core"/"orchestrator.py", r"uses_backticks", 30))

print("\n" + "="*70)
print("DIAG: core/browser.py — _verify_paste")
print("="*70)
print(extract_section(ROOT/"core"/"browser.py", r"def _verify_paste", 30))