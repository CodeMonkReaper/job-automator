"""Configuración centralizada y rutas portables para Job Automator."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

PACKAGE_ROOT = Path(__file__).resolve().parent
DEFAULT_PROJECT_ROOT = PACKAGE_ROOT.parent.parent


def get_workspace_root() -> Path:
    """Retorna la raíz del workspace resuelta dinámicamente desde entorno o paquete."""
    raw = os.environ.get("JOB_AUTOMATOR_WORKSPACE")
    return Path(raw).resolve() if raw else DEFAULT_PROJECT_ROOT


def get_config_dir() -> Path:
    """Retorna el directorio de configuraciones respetando JOB_AUTOMATOR_CONFIG_DIR."""
    raw = os.environ.get("JOB_AUTOMATOR_CONFIG_DIR")
    return Path(raw).resolve() if raw else (get_workspace_root() / "config")


def get_data_dir() -> Path:
    """Retorna el directorio de persistencia respetando JOB_AUTOMATOR_DATA_DIR."""
    raw = os.environ.get("JOB_AUTOMATOR_DATA_DIR")
    return Path(raw).resolve() if raw else (get_workspace_root() / "data")


def get_logs_dir() -> Path:
    """Retorna el directorio de logs estructurados."""
    return get_data_dir() / "logs"


def get_output_dir() -> Path:
    """Retorna el directorio de expedientes, reportes y CVs generados."""
    raw = os.environ.get("JOB_AUTOMATOR_OUTPUT_DIR")
    return Path(raw).resolve() if raw else (get_workspace_root() / "output")


def get_cvs_dir() -> Path:
    """Retorna el directorio donde viven los CVs base del usuario."""
    raw = os.environ.get("JOB_AUTOMATOR_CVS_DIR")
    return Path(raw).resolve() if raw else (get_workspace_root() / "cvs")


def get_db_path() -> Path:
    """Retorna la ruta a la base de datos SQLite respetando JOB_AUTOMATOR_DB_PATH."""
    raw = os.environ.get("JOB_AUTOMATOR_DB_PATH")
    return Path(raw).resolve() if raw else (get_data_dir() / "jobs.db")


WORKSPACE_ROOT = get_workspace_root()
ENGINE_ROOT = WORKSPACE_ROOT
CONFIG_DIR = get_config_dir()
DATA_DIR = get_data_dir()
LOGS_DIR = get_logs_dir()
OUTPUT_DIR = get_output_dir()
CVS_DIR = get_cvs_dir()
CVS_ACTUALIZADOS_DIR = CVS_DIR
LEGACY_SCRIPTS_DIR = WORKSPACE_ROOT / "scripts"
DEFAULT_DB_PATH = get_db_path()
SCHEMA_PATH = PACKAGE_ROOT / "db" / "schema.sql"


def ensure_directories() -> None:
    """Garantiza la existencia de los directorios de datos, logs y salida."""
    get_data_dir().mkdir(parents=True, exist_ok=True)
    get_logs_dir().mkdir(parents=True, exist_ok=True)
    get_output_dir().mkdir(parents=True, exist_ok=True)


def load_json_config(filename: str) -> dict[str, Any]:
    """Carga un JSON desde config/ o hace fallback a *.example.json si aún no existe."""
    cfg_dir = get_config_dir()
    primary = cfg_dir / filename
    if primary.exists():
        with open(primary, "r", encoding="utf-8") as f:
            return json.load(f)

    if filename.endswith(".json") and not filename.endswith(".example.json"):
        example = cfg_dir / f"{filename[:-5]}.example.json"
        if example.exists():
            with open(example, "r", encoding="utf-8") as f:
                return json.load(f)

    with open(primary, "r", encoding="utf-8") as f:
        return json.load(f)


def get_candidate_profile() -> dict[str, Any]:
    """Carga el perfil oficial del candidato desde candidate_profile[.example].json."""
    return load_json_config("candidate_profile.json")


def get_search_preferences() -> dict[str, Any]:
    """Carga las preferencias de búsqueda desde search_preferences[.example].json."""
    return load_json_config("search_preferences.json")


def get_cv_catalog() -> dict[str, Any]:
    """Carga el catálogo de CVs desde cv_catalog[.example].json."""
    return load_json_config("cv_catalog.json")
