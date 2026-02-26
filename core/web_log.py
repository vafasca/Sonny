"""
core/web_log.py  v2 — Sistema de logging Sonny

v2 — NUEVO:
  - JSONL paralelo: cada evento → una línea JSON en logs/sessions.jsonl
  - Nuevos eventos: log_build_error, log_fix_applied, log_autofix,
    log_dependency_warning, log_session_end
  - query_sessions() + get_error_stats() + get_session_success_rate()
  - Backward compatible con v1 (text log sigue funcionando)
"""

import json
import os
from pathlib import Path
from datetime import datetime

_LOG_DIR   = Path(__file__).parent.parent / "logs"
_LOG_TXT   = _LOG_DIR / "web_orchestrator.log"
_LOG_JSONL = _LOG_DIR / "sessions.jsonl"
_LOG_DIR.mkdir(parents=True, exist_ok=True)

def _ts() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

def _ts_iso() -> str:
    return datetime.now().isoformat()

def _append_txt(text: str):
    try:
        with open(_LOG_TXT, "a", encoding="utf-8") as f:
            f.write(text + "\n")
    except Exception:
        pass

def _append_jsonl(event: dict):
    try:
        event["ts"] = _ts_iso()
        with open(_LOG_JSONL, "a", encoding="utf-8") as f:
            f.write(json.dumps(event, ensure_ascii=False) + "\n")
    except Exception:
        pass


def log_session_start(objetivo: str):
    sep = "=" * 70
    _append_txt(f"\n{sep}\n# NUEVA SESIÓN ORQUESTADOR WEB — {_ts()}\n# Objetivo: {objetivo}\n{sep}\n")
    _append_jsonl({"event": "session_start", "objetivo": objetivo})


def log_prompt(site: str, prompt: str, objetivo: str):
    sep = "=" * 70
    _append_txt(f"\n{sep}\n[{_ts()}]  ▶ PROMPT ENVIADO → {site}\n{sep}\nOBJETIVO USUARIO : {objetivo}\n{'─'*70}\nPROMPT ENVIADO   :\n{prompt}\n")
    _append_jsonl({"event": "prompt_sent", "site": site, "objetivo": objetivo, "prompt_len": len(prompt)})


def log_response(site: str, response: str, num_steps: int = 0):
    sep = "=" * 70
    _append_txt(f"\n[{_ts()}]  ◀ RESPUESTA RECIBIDA ← {site}\n{'─'*70}\nCARACTERES: {len(response)} | PASOS DETECTADOS: {num_steps}\nRESPUESTA  :\n{response}\n{sep}\n")
    _append_jsonl({"event": "response_received", "site": site, "response_len": len(response), "num_steps": num_steps})


def log_error(site: str, error: str):
    _append_txt(f"\n[{_ts()}]  ❌ ERROR [{site}]: {error}\n")
    _append_jsonl({"event": "error", "site": site, "error": error[:500]})


# ─── FASE 2 — Nuevos eventos ─────────────────────────────────────────────────

def log_build_error(round_num: int, error_codes: list, error_text: str, ng_major: int = 0):
    _append_txt(f"\n[{_ts()}]  🔨 BUILD ERROR (ronda {round_num}): {', '.join(error_codes)}\n")
    _append_jsonl({"event": "build_error", "round": round_num, "error_codes": error_codes,
                   "ng_major": ng_major, "error_preview": error_text[:500]})


def log_fix_applied(round_num: int, files_changed: list, strategy: str = "normal"):
    _append_txt(f"\n[{_ts()}]  🔧 FIX APLICADO (ronda {round_num}, {strategy}): {', '.join(files_changed[:5])}\n")
    _append_jsonl({"event": "fix_applied", "round": round_num, "strategy": strategy,
                   "files_changed": files_changed, "files_count": len(files_changed)})


def log_autofix(fix_type: str, files: list, ng_major: int = 0):
    _append_txt(f"\n[{_ts()}]  ⚙️  AUTOFIX [{fix_type}]: {', '.join(files)}\n")
    _append_jsonl({"event": "autofix", "fix_type": fix_type, "files": files, "ng_major": ng_major})


def log_dependency_warning(warnings: list):
    _append_txt(f"\n[{_ts()}]  ⚠️  DEPENDENCY WARNINGS:\n" + "\n".join(f"  • {w}" for w in warnings) + "\n")
    _append_jsonl({"event": "dependency_warning", "warnings": warnings, "count": len(warnings)})


def log_session_end(objetivo: str, success: bool, total_rounds: int = 0, ng_major: int = 0):
    status = "✅ ÉXITO" if success else "❌ FALLÓ"
    _append_txt(f"\n[{_ts()}]  {status} — sesión finalizada\n  Objetivo: {objetivo}\n  Rondas de fix: {total_rounds}\n  Angular v{ng_major}\n")
    _append_jsonl({"event": "session_end", "objetivo": objetivo, "success": success,
                   "total_rounds": total_rounds, "ng_major": ng_major})


# ─── Utilidades de análisis ───────────────────────────────────────────────────

def query_sessions(event_type: str = None, last_n: int = 50) -> list:
    if not _LOG_JSONL.exists():
        return []
    try:
        lines = _LOG_JSONL.read_text(encoding="utf-8").strip().splitlines()
        results = []
        for line in lines[-last_n:]:
            try:
                entry = json.loads(line)
                if event_type is None or entry.get("event") == event_type:
                    results.append(entry)
            except json.JSONDecodeError:
                pass
        return results
    except Exception:
        return []


def get_error_stats() -> dict:
    errors = query_sessions("build_error", last_n=500)
    code_count: dict = {}
    for e in errors:
        for code in e.get("error_codes", []):
            code_count[code] = code_count.get(code, 0) + 1
    return dict(sorted(code_count.items(), key=lambda x: -x[1]))


def get_session_success_rate() -> dict:
    sessions = query_sessions("session_end", last_n=100)
    if not sessions:
        return {"total": 0, "success": 0, "rate": 0.0}
    total   = len(sessions)
    success = sum(1 for s in sessions if s.get("success"))
    return {"total": total, "success": success, "failed": total - success,
            "rate": round(success / total * 100, 1)}