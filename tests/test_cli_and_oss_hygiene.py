"""Pruebas de CLI portable y verificación de higiene OSS (cero PII o secretos)."""

from __future__ import annotations

import json
from pathlib import Path
import re
import shutil

import pymupdf
import pytest

from job_automator import cli


THIS_FILE = Path(__file__).resolve()
OSS_ROOT = THIS_FILE.parent.parent


def test_cli_init_creates_workspace_configs_and_env(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Verifica `job-automator init` en un workspace temporal sin tocar config/ del repo."""
    ws = tmp_path / "isolated_ws"
    cfg = ws / "config"
    cfg.mkdir(parents=True)

    repo_cfg = OSS_ROOT / "config"
    for name in (
        "candidate_profile.example.json",
        "cv_catalog.example.json",
        "search_preferences.example.json",
    ):
        shutil.copy2(repo_cfg / name, cfg / name)
    shutil.copy2(OSS_ROOT / ".env.example", ws / ".env.example")

    monkeypatch.setenv("JOB_AUTOMATOR_WORKSPACE", str(ws))
    monkeypatch.setenv("JOB_AUTOMATOR_CONFIG_DIR", str(cfg))
    monkeypatch.setenv("JOB_AUTOMATOR_DATA_DIR", str(ws / "data"))
    monkeypatch.setenv("JOB_AUTOMATOR_OUTPUT_DIR", str(ws / "output"))
    monkeypatch.setenv("JOB_AUTOMATOR_CVS_DIR", str(ws / "cvs"))
    monkeypatch.setenv("JOB_AUTOMATOR_DB_PATH", str(ws / "data" / "jobs.db"))

    exit_code = cli.main(["init"])
    assert exit_code == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert (cfg / "candidate_profile.json").exists()
    assert (cfg / "cv_catalog.json").exists()
    assert (cfg / "search_preferences.json").exists()
    assert (ws / ".env").exists()


def test_cli_doctor_evaluate_and_generate_cv(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Verifica los subcomandos doctor, evaluate y generate-cv de la CLI."""
    assert cli.main(["doctor"]) == 0
    doctor_out = json.loads(capsys.readouterr().out)
    assert doctor_out["ok"] is True
    assert doctor_out["configs"]["candidate_profile"] in ("active", "example_fallback")
    assert doctor_out["playwright"]["imported"] is True

    assert (
        cli.main(
            [
                "evaluate",
                "--title",
                "Desarrollador Backend Python",
                "--desc",
                "FastAPI PostgreSQL Docker",
            ]
        )
        == 0
    )
    eval_out = json.loads(capsys.readouterr().out)
    assert eval_out["ok"] is True
    assert eval_out["eligible"] is True
    assert eval_out["decision"] == "ELIGIBLE"

    pdf_out = tmp_path / "cli_smoke_cv.pdf"
    assert (
        cli.main(
            [
                "generate-cv",
                "--title",
                "Desarrollador Full Stack",
                "--desc",
                "Python React PostgreSQL Docker",
                "--out",
                str(pdf_out),
            ]
        )
        == 0
    )
    cv_out = json.loads(capsys.readouterr().out)
    assert cv_out["ok"] is True
    assert cv_out["page_count"] == 1
    assert pdf_out.exists()

    doc = pymupdf.open(str(pdf_out))
    assert doc.page_count == 1
    doc.close()


def test_oss_repository_has_no_pii_or_secrets() -> None:
    """Escanea todo job-automator-oss/ verificando ausencia de PII, rutas personales y legacy."""
    forbidden_re = re.compile(
        r"luis_|salamanca|3501|hotmail|onedrive|c:\\users\\luis_|"
        r"duoc|pymatica|masterbase|nomall|codemonkreaper|vetbiob|"
        r"margenclaro|sidep|aiza|scratch\.",
        re.IGNORECASE,
    )
    skip_dirs = {".git", "__pycache__", ".pytest_cache", ".venv", "venv"}
    binary_suffixes = {".pdf", ".docx", ".png", ".jpg", ".jpeg", ".pyc", ".db", ".sqlite", ".sqlite3"}

    hits: list[str] = []
    for path in sorted(OSS_ROOT.rglob("*")):
        if not path.is_file():
            continue
        if path.resolve() == THIS_FILE:
            continue
        if any(part in skip_dirs for part in path.parts):
            continue
        if path.suffix.lower() in binary_suffixes:
            continue

        content = path.read_text(encoding="utf-8", errors="ignore")
        for line_no, line in enumerate(content.splitlines(), 1):
            if forbidden_re.search(line):
                rel = path.relative_to(OSS_ROOT)
                hits.append(f"{rel}:{line_no}: {line.strip()[:120]}")

    assert hits == [], f"Se detectaron referencias PII o legacy en job-automator-oss/: {hits}"
    assert not (OSS_ROOT / ".env").exists(), "No debe existir .env en la plantilla OSS."
    assert not list(OSS_ROOT.glob("data/*.db")), "No deben existir bases de datos .db en la plantilla OSS."
