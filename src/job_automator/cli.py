"""Interfaz de línea de comandos (CLI) portable para Job Automator."""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path
from typing import Any, Sequence

import pymupdf

from job_automator.browser.cdp_manager import discover_cdp_endpoint
from job_automator.config import (
    ensure_directories,
    get_config_dir,
    get_cvs_dir,
    get_data_dir,
    get_output_dir,
    get_workspace_root,
)
from job_automator.db.repository import JobRepository
from job_automator.intelligence.ats_cv_generator import (
    _find_playwright_executable,
    generate_ats_cv_for_job,
)
from job_automator.intelligence.evaluator import evaluate_offer
from job_automator.reporting.report_builder import build_consolidated_report


def cmd_init(force: bool = False) -> dict[str, Any]:
    """Inicializa directorios de trabajo y copia config/*.example.json a config/*.json."""
    ensure_directories()
    cfg_dir = get_config_dir()
    cfg_dir.mkdir(parents=True, exist_ok=True)
    cvs_dir = get_cvs_dir()
    cvs_dir.mkdir(parents=True, exist_ok=True)

    copied: list[str] = []
    skipped: list[str] = []
    missing_examples: list[str] = []

    for base in ("candidate_profile", "cv_catalog", "search_preferences"):
        example = cfg_dir / f"{base}.example.json"
        target = cfg_dir / f"{base}.json"
        if not example.exists():
            missing_examples.append(str(example))
            continue
        if target.exists() and not force:
            skipped.append(str(target))
        else:
            shutil.copy2(example, target)
            copied.append(str(target))

    ws_root = get_workspace_root()
    env_example = ws_root / ".env.example"
    env_target = ws_root / ".env"
    if env_example.exists():
        if env_target.exists() and not force:
            skipped.append(str(env_target))
        else:
            shutil.copy2(env_example, env_target)
            copied.append(str(env_target))

    return {
        "ok": len(missing_examples) == 0,
        "config_dir": str(cfg_dir),
        "data_dir": str(get_data_dir()),
        "output_dir": str(get_output_dir()),
        "cvs_dir": str(cvs_dir),
        "copied": copied,
        "skipped": skipped,
        "missing_examples": missing_examples,
    }


def cmd_doctor() -> dict[str, Any]:
    """Verifica Python, configuraciones, Playwright/Chromium y disponibilidad de Chrome CDP."""
    cfg_dir = get_config_dir()
    configs_status: dict[str, str] = {}
    for base in ("candidate_profile", "cv_catalog", "search_preferences"):
        live = cfg_dir / f"{base}.json"
        example = cfg_dir / f"{base}.example.json"
        if live.exists():
            configs_status[base] = "active"
        elif example.exists():
            configs_status[base] = "example_fallback"
        else:
            configs_status[base] = "missing"

    playwright_ok = False
    playwright_error = None
    chromium_exe = _find_playwright_executable()
    try:
        import playwright  # noqa: F401
        playwright_ok = True
    except Exception as exc:
        playwright_error = str(exc)

    cdp_info = discover_cdp_endpoint()
    all_configs_present = all(v != "missing" for v in configs_status.values())
    overall_ok = sys.version_info >= (3, 11) and all_configs_present and playwright_ok

    return {
        "ok": overall_ok,
        "python_version": sys.version.split()[0],
        "config_dir": str(cfg_dir),
        "configs": configs_status,
        "playwright": {
            "imported": playwright_ok,
            "chromium_executable": chromium_exe,
            "error": playwright_error,
        },
        "cdp": {
            "available": cdp_info.available,
            "source": cdp_info.source,
            "error": cdp_info.error,
        },
    }


def cmd_evaluate(title: str, desc: str = "", location: str = "", modality: str = "") -> dict[str, Any]:
    """Evalúa la compatibilidad de una oferta laboral contra las preferencias configuradas."""
    res = evaluate_offer(title=title, description=desc, location=location, modality=modality)
    return {
        "ok": True,
        "eligible": res.eligible,
        "score": res.score,
        "decision": res.decision,
        "reason": res.reason,
        "detected_modality": res.detected_modality,
    }


def cmd_generate_cv(
    title: str,
    desc: str = "",
    out: str | None = None,
    company: str = "",
    app_id: int | None = None,
) -> dict[str, Any]:
    """Genera un CV personalizado ATS en PDF de 1 página y verifica su conteo de páginas."""
    output_path = Path(out).resolve() if out else None
    res = generate_ats_cv_for_job(
        job_title=title,
        job_description=desc,
        output_path=output_path,
        company_name=company,
        app_id=app_id,
    )
    if not res.ok or not res.pdf_path:
        return {
            "ok": False,
            "error": res.error or "Error desconocido generando CV ATS.",
        }

    doc = pymupdf.open(str(res.pdf_path))
    page_count = len(doc)
    doc.close()

    return {
        "ok": page_count == 1,
        "pdf_path": str(res.pdf_path),
        "page_count": page_count,
        "ats_score": res.ats_score,
        "matched_keywords": res.matched_keywords,
        "selected_projects": [p.get("name") for p in res.selected_projects],
    }


def cmd_report(no_write: bool = False) -> dict[str, Any]:
    """Genera o consulta el informe consolidado desde SQLite."""
    repo = JobRepository()
    return build_consolidated_report(repo=repo, write_markdown=not no_write)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="job-automator",
        description="CLI de Job Automator: inicialización, diagnóstico, evaluación ATS y servidor MCP.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_init = sub.add_parser("init", help="Inicializa directorios y copia plantillas config/*.example.json")
    p_init.add_argument("--force", action="store_true", help="Sobrescribe archivos config/*.json existentes")

    sub.add_parser("doctor", help="Verifica Python, configuraciones, Playwright y Chrome CDP")

    p_eval = sub.add_parser("evaluate", help="Evalúa elegibilidad de una oferta laboral")
    p_eval.add_argument("--title", required=True, help="Título del cargo")
    p_eval.add_argument("--desc", default="", help="Descripción de la oferta")
    p_eval.add_argument("--location", default="", help="Ubicación de la oferta")
    p_eval.add_argument("--modality", default="", help="Modalidad de trabajo")

    p_cv = sub.add_parser("generate-cv", help="Genera un CV personalizado ATS de 1 página en PDF")
    p_cv.add_argument("--title", required=True, help="Título del cargo objetivo")
    p_cv.add_argument("--desc", default="", help="Descripción o requisitos del cargo")
    p_cv.add_argument("--out", default=None, help="Ruta destino del PDF o carpeta")
    p_cv.add_argument("--company", default="", help="Nombre de la empresa")
    p_cv.add_argument("--app-id", type=int, default=None, help="ID opcional de postulación")

    p_rep = sub.add_parser("report", help="Muestra métricas de SQLite y actualiza APPLICATION_REPORT.md")
    p_rep.add_argument("--no-write", action="store_true", help="Solo muestra métricas sin escribir Markdown")

    sub.add_parser("mcp", help="Inicia el servidor MCP por stdio")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command == "init":
        payload = cmd_init(force=args.force)
    elif args.command == "doctor":
        payload = cmd_doctor()
    elif args.command == "evaluate":
        payload = cmd_evaluate(
            title=args.title,
            desc=args.desc,
            location=args.location,
            modality=args.modality,
        )
    elif args.command == "generate-cv":
        payload = cmd_generate_cv(
            title=args.title,
            desc=args.desc,
            out=args.out,
            company=args.company,
            app_id=args.app_id,
        )
    elif args.command == "report":
        payload = cmd_report(no_write=args.no_write)
    elif args.command == "mcp":
        from job_automator.mcp_server import main as mcp_main
        mcp_main()
        return 0
    else:
        parser.print_help()
        return 2

    print(json.dumps(payload, indent=2, ensure_ascii=False))
    return 0 if payload.get("ok", True) else 1


if __name__ == "__main__":
    raise SystemExit(main())
