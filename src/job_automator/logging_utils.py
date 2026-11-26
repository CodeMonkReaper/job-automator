"""Utilidades de logging estructurado con sanitización de secretos y cookies."""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from job_automator.config import ensure_directories, get_logs_dir

SENSITIVE_PATTERNS = [
    (re.compile(r"(password|passwd|secret|token|cookie|authorization)\s*[:=]\s*['\"]?([^'\"\s,;]+)", re.I), r"\1=***REDACTED***"),
    (re.compile(r"(ws://127\.0\.0\.1:\d+/devtools/browser/)[a-f0-9\-]+", re.I), r"\1***TOKEN_REDACTED***"),
]


def sanitize_text(value: str) -> str:
    """Oculta tokens CDP, cookies o credenciales de cualquier texto de log."""
    result = value
    for pattern, repl in SENSITIVE_PATTERNS:
        result = pattern.sub(repl, result)
    return result


def sanitize_obj(obj: Any) -> Any:
    if isinstance(obj, str):
        return sanitize_text(obj)
    if isinstance(obj, dict):
        return {
            k: ("***REDACTED***" if any(s in k.lower() for s in ("password", "cookie", "secret", "token")) else sanitize_obj(v))
            for k, v in obj.items()
        }
    if isinstance(obj, list):
        return [sanitize_obj(x) for x in obj]
    return obj


def log_event(
    component: str,
    event: str,
    level: str = "INFO",
    session_id: str | None = None,
    application_id: int | None = None,
    details: dict[str, Any] | None = None,
    log_dir: Path | None = None,
) -> dict[str, Any]:
    """Escribe un evento JSONL sanitizado en el archivo de log del día."""
    ensure_directories()
    target_dir = log_dir or get_logs_dir()
    target_dir.mkdir(parents=True, exist_ok=True)
    day_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    log_file = target_dir / f"automator_{day_str}.jsonl"

    record = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "level": level.upper(),
        "component": component,
        "event": event,
        "session_id": session_id,
        "application_id": application_id,
        "details": sanitize_obj(details or {}),
    }
    with open(log_file, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")
    return record
