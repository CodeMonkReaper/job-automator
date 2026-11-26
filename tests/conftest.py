"""Fixtures compartidas para aislar las pruebas en directorios temporales sin PII."""

from __future__ import annotations

import json
from pathlib import Path
import pytest


OSS_ROOT = Path(__file__).resolve().parent.parent
OSS_CONFIG_DIR = OSS_ROOT / "config"


@pytest.fixture(autouse=True)
def isolated_oss_workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Aísla workspace, datos, CVs y salida en tmp_path usando config/*.example.json."""
    data_dir = tmp_path / "data"
    output_dir = tmp_path / "output"
    cvs_dir = tmp_path / "cvs"

    data_dir.mkdir(parents=True, exist_ok=True)
    output_dir.mkdir(parents=True, exist_ok=True)
    cvs_dir.mkdir(parents=True, exist_ok=True)

    catalog_example = OSS_CONFIG_DIR / "cv_catalog.example.json"
    cv_bases: set[str] = {"CV_Candidate_Software_Engineer"}
    if catalog_example.exists():
        catalog_data = json.loads(catalog_example.read_text(encoding="utf-8"))
        if catalog_data.get("default_cv_base"):
            cv_bases.add(str(catalog_data["default_cv_base"]))
        for profile in catalog_data.get("profiles", []):
            if profile.get("cv_base"):
                cv_bases.add(str(profile["cv_base"]))

    for base in sorted(cv_bases):
        (cvs_dir / f"{base}.pdf").write_bytes(b"%PDF-1.4\n% Dummy ATS Test CV\n%%EOF\n")
        (cvs_dir / f"{base}.docx").write_bytes(b"PK\x03\x04Dummy DOCX Test CV")

    monkeypatch.setenv("JOB_AUTOMATOR_WORKSPACE", str(tmp_path))
    monkeypatch.setenv("JOB_AUTOMATOR_CONFIG_DIR", str(OSS_CONFIG_DIR))
    monkeypatch.setenv("JOB_AUTOMATOR_DATA_DIR", str(data_dir))
    monkeypatch.setenv("JOB_AUTOMATOR_OUTPUT_DIR", str(output_dir))
    monkeypatch.setenv("JOB_AUTOMATOR_CVS_DIR", str(cvs_dir))
    monkeypatch.setenv("JOB_AUTOMATOR_DB_PATH", str(data_dir / "jobs.db"))

    return tmp_path
