"""Gestor de conexión Playwright sobre Chrome CDP (cumpliendo Regla 1: Prohibido taskkill)."""

from __future__ import annotations

import json
import os
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from job_automator.logging_utils import log_event


@dataclass
class CDPEndpointInfo:
    available: bool
    ws_url: str | None
    source: str
    error: str | None = None


def sanitize_cdp_chrome_targets(port: int = 9222) -> None:
    """Neutraliza pestañas internas de Chrome (ej. chrome://newtab) que bloquean Page.enable en Playwright."""
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/list", timeout=2.0) as resp:
            targets = json.loads(resp.read().decode("utf-8"))
        has_normal_page = False
        tabs_to_close = []
        for t in targets:
            url = t.get("url", "")
            tid = t.get("id")
            t_type = t.get("type")
            if t_type == "page":
                if url.startswith("chrome://newtab") or url == "chrome://new-tab-page/":
                    tabs_to_close.append(tid)
                else:
                    has_normal_page = True
        # Si no hay ninguna página normal abierta, abrir about:blank antes de cerrar newtab
        if not has_normal_page and tabs_to_close:
            try:
                req_new = urllib.request.Request(f"http://127.0.0.1:{port}/json/new?about:blank", method="PUT")
                urllib.request.urlopen(req_new, timeout=1.5)
            except Exception:
                pass
        for tid in tabs_to_close:
            try:
                req_c = urllib.request.Request(f"http://127.0.0.1:{port}/json/close/{tid}", method="PUT")
                urllib.request.urlopen(req_c, timeout=1.0)
            except Exception:
                pass
    except Exception:
        pass


def discover_cdp_endpoint(port: int | None = None) -> CDPEndpointInfo:
    """
    Descubre el WebSocket CDP activo de la sesión real de Chrome del usuario.
    PROHIBIDO matar procesos o iniciar perfiles vacíos.
    """
    target_port = int(port or os.environ.get("JOB_AUTOMATOR_CDP_PORT", "9222"))
    sanitize_cdp_chrome_targets(target_port)
    # 1. Intentar consultar http://127.0.0.1:<port>/json/version si el puerto está escuchando
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{target_port}/json/version", timeout=2.0) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            ws = data.get("webSocketDebuggerUrl")
            if ws:
                return CDPEndpointInfo(available=True, ws_url=ws, source=f"http_{target_port}_json_version")
    except Exception:
        pass

    # 2. Leer DevToolsActivePort de rutas portables multiplataforma (Windows / macOS / Linux)
    candidate_paths: list[Path] = []
    custom_debug_dir = os.environ.get("JOB_AUTOMATOR_CHROME_DEBUG_DIR")
    if custom_debug_dir:
        candidate_paths.append(Path(custom_debug_dir) / "DevToolsActivePort")
    candidate_paths.append(Path.home() / "ChromeDebug" / "DevToolsActivePort")
    if os.name == "nt":
        local_app = os.environ.get("LOCALAPPDATA")
        if local_app:
            candidate_paths.append(Path(local_app) / "Google" / "Chrome" / "User Data" / "DevToolsActivePort")
    candidate_paths.extend([
        Path.home() / "Library" / "Application Support" / "Google" / "Chrome" / "DevToolsActivePort",
        Path.home() / ".config" / "google-chrome" / "DevToolsActivePort",
        Path.home() / ".config" / "chromium" / "DevToolsActivePort",
    ])
    for active_port_path in candidate_paths:
        if active_port_path.exists():
            try:
                lines = [ln.strip() for ln in active_port_path.read_text(encoding="utf-8").splitlines() if ln.strip()]
                if len(lines) >= 2:
                    found_port, path = lines[0], lines[1]
                    try:
                        with urllib.request.urlopen(f"http://127.0.0.1:{found_port}/json/version", timeout=1.5) as resp:
                            data = json.loads(resp.read().decode("utf-8"))
                            ws_url = data.get("webSocketDebuggerUrl") or f"ws://127.0.0.1:{found_port}{path}"
                            return CDPEndpointInfo(available=True, ws_url=ws_url, source=f"DevToolsActivePort_{active_port_path.name}")
                    except Exception:
                        pass
            except Exception:
                pass

    log_event("cdp_manager", "cdp_not_available", level="WARNING")
    return CDPEndpointInfo(
        available=False,
        ws_url=None,
        source="none",
        error=f"No se detectó Chrome con puerto de depuración activo (DevToolsActivePort o puerto {target_port}).",
    )
