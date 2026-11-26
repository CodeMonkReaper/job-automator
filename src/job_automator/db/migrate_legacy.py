"""Migrador idempotente de carpetas históricas de postulaciones y applied_history.json a SQLite."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from job_automator.config import get_output_dir, get_workspace_root
from job_automator.db.repository import JobRepository, utc_now_iso

EXCLUDED_DIRS = {
    "cvs",
    "config",
    "data",
    "output",
    "scripts",
    "src",
    "tests",
    ".agent",
    ".agents",
    ".gemini",
    ".github",
    ".git",
    "__pycache__",
}


def parse_salary_clp(text: str) -> int:
    """Extrae un monto entero en CLP desde un string como '$750.000 líquidos'."""
    if not text:
        return 800000
    cleaned = text.replace(".", "").replace(",", "")
    m = re.search(r"(\d{6,7})", cleaned)
    if m:
        return int(m.group(1))
    return 800000


def normalize_portal_name(raw_portal: str, url: str) -> str:
    p = (raw_portal or "").lower()
    u = (url or "").lower()
    if "computrabajo" in p or "computrabajo" in u:
        return "computrabajo"
    if "indeed" in p or "indeed" in u:
        return "indeed"
    if "get on board" in p or "getonbrd" in p or "getonbrd" in u:
        return "getonboard"
    if "bne" in p or "bne.cl" in u:
        return "bne"
    return "chiletrabajos"


def extract_external_id(portal: str, url: str) -> str | None:
    if not url:
        return None
    if portal == "chiletrabajos":
        m = re.search(r"(\d{6,8})(?:$|/|\?)", url)
        return m.group(1) if m else None
    if portal == "indeed":
        m = re.search(r"jk=([a-f0-9]+)", url, re.I)
        return m.group(1) if m else None
    if portal == "computrabajo":
        m = re.search(r"-([A-F0-9]{20,36})(?:$|#|\?)", url, re.I)
        return m.group(1) if m else None
    return None


def migrate_legacy_workspace(
    repo: JobRepository | None = None,
    workspace_root: Path | None = None,
) -> dict[str, Any]:
    """Importa carpetas existentes de postulaciones a SQLite."""
    repository = repo or JobRepository()
    scan_root = workspace_root or get_output_dir()
    if not scan_root.exists():
        scan_root = get_workspace_root()
    folders = [
        d
        for d in sorted(scan_root.iterdir(), key=lambda x: x.name.lower())
        if d.is_dir() and d.name not in EXCLUDED_DIRS and not d.name.startswith(".")
    ]

    imported_count = 0
    confirmed_count = 0
    closed_count = 0
    pending_count = 0

    for folder_path in folders:
        folder_name = folder_path.name
        files = {f.name for f in folder_path.iterdir() if f.is_file()}

        has_conf = "confirmacion_postulacion.png" in files
        has_form = "pantallazo_respuestas_formulario.png" in files
        has_txt = "respuestas_postulacion.txt" in files
        has_md = "DETALLES_OFERTA.md" in files

        if not (has_conf or has_txt or has_md):
            continue

        company = folder_name.split(" - ")[0].strip() if " - " in folder_name else folder_name
        title = folder_name.split(" - ", 1)[1].strip() if " - " in folder_name else folder_name
        raw_portal = "Chiletrabajos"
        raw_salary = "$800.000"
        url = ""
        is_closed = False
        is_bne_pending = False

        if has_md:
            try:
                content = (folder_path / "DETALLES_OFERTA.md").read_text(encoding="utf-8", errors="replace")
                if "CERRADA EN PORTAL" in content.upper():
                    is_closed = True
                if "PENDIENTE" in content.upper() and "BNE" in content.upper():
                    is_bne_pending = True
                m_corp = re.search(r"\*\*Empresa:\*\*\s*(.*)", content)
                if m_corp:
                    company = m_corp.group(1).strip()
                m_cargo = re.search(r"\*\*Cargo:\*\*\s*(.*)", content)
                if m_cargo:
                    title = m_cargo.group(1).strip()
                m_portal = re.search(r"\*\*Portal.*?:\*\*\s*(.*)", content)
                if m_portal:
                    raw_portal = m_portal.group(1).strip()
                m_renta = re.search(r"\*\*Renta Pretendida.*?:\*\*\s*(.*)", content)
                if m_renta:
                    raw_salary = m_renta.group(1).strip()
                m_url = re.search(r"\*\*(?:Enlace Oficial|URL(?: Postulación)?):\*\*\s*(\S+)", content)
                if m_url:
                    url = m_url.group(1).strip()
            except Exception:
                pass

        answers_txt = ""
        if has_txt:
            try:
                answers_txt = (folder_path / "respuestas_postulacion.txt").read_text(
                    encoding="utf-8", errors="replace"
                )
                if not url:
                    m_u = re.search(r"URL:\s*(http\S+)", answers_txt)
                    if m_u:
                        url = m_u.group(1).strip()
                m_p = re.search(r"PORTAL:\s*(.*)", answers_txt)
                if m_p and raw_portal == "Chiletrabajos":
                    raw_portal = m_p.group(1).strip()
                m_r = re.search(r"RENTA:\s*(.*)", answers_txt)
                if m_r and raw_salary == "$800.000":
                    raw_salary = m_r.group(1).strip()
            except Exception:
                pass

        portal = normalize_portal_name(raw_portal, url)
        if not url:
            slug = re.sub(r"[^a-z0-9]+", "-", folder_name.lower()).strip("-")
            url = f"https://legacy.local/{portal}/{slug}"

        ext_id = extract_external_id(portal, url)
        salary_clp = parse_salary_clp(raw_salary)

        if is_closed:
            state = "CLOSED_ON_PORTAL"
            match_decision = "CLOSED_OFFER"
            closed_count += 1
        elif is_bne_pending or (portal == "bne" and not has_conf):
            state = "NEEDS_HUMAN_REVIEW"
            match_decision = "ELIGIBLE"
            pending_count += 1
        elif has_conf:
            state = "CONFIRMED"
            match_decision = "ELIGIBLE"
            confirmed_count += 1
        else:
            state = "SUBMITTED_UNVERIFIED"
            match_decision = "ELIGIBLE"
            pending_count += 1

        pdf_file = next((f for f in files if f.lower().endswith(".pdf")), None)
        docx_file = next((f for f in files if f.lower().endswith(".docx")), None)
        cv_target = pdf_file[:-4] if pdf_file else "CV_Candidate"

        _, app_id = repository.register_offer_and_application(
            portal=portal,
            url=url,
            company_name=company,
            title=title,
            external_job_id=ext_id,
            apply_url=url,
            raw_salary_text=raw_salary,
            match_score=0.95,
            match_decision=match_decision,
            initial_state=state,
            selected_cv_base=cv_target,
            selected_cv_target=cv_target,
            folder_name=folder_name,
            folder_path=str(folder_path),
            expected_salary_clp=salary_clp,
            cover_letter_text=answers_txt[:2000] if answers_txt else None,
            is_legacy_imported=True,
            confirmed_at=utc_now_iso() if state == "CONFIRMED" else None,
        )

        # Registrar evidencia existente si la aplicación recién se importó
        if has_conf:
            repository.add_evidence(app_id, "CONFIRMATION_SCREENSHOT", folder_path / "confirmacion_postulacion.png")
        if has_form:
            repository.add_evidence(app_id, "FORM_SCREENSHOT", folder_path / "pantallazo_respuestas_formulario.png")
        if has_md:
            repository.add_evidence(app_id, "DOSSIER_MD", folder_path / "DETALLES_OFERTA.md")
        if has_txt:
            repository.add_evidence(app_id, "ANSWERS_TXT", folder_path / "respuestas_postulacion.txt")
        if pdf_file:
            repository.add_evidence(app_id, "CV_PDF", folder_path / pdf_file)
        if docx_file:
            repository.add_evidence(app_id, "CV_DOCX", folder_path / docx_file)

        imported_count += 1

    # También importar IDs adicionales de applied_history.json que no tengan carpeta
    history_file = get_workspace_root() / "scripts" / "applied_history.json"
    extra_history_ids = 0
    if history_file.exists():
        try:
            hist = json.loads(history_file.read_text(encoding="utf-8"))
            for jid in hist.get("applied_ids", []):
                url = f"https://www.chiletrabajos.cl/trabajo/postular/{jid}"
                is_dup, _ = repository.is_offer_duplicate("chiletrabajos", str(jid), url, "Histórico", f"Oferta {jid}")
                if not is_dup:
                    repository.register_offer_and_application(
                        portal="chiletrabajos",
                        url=url,
                        company_name="Registro Histórico Chiletrabajos",
                        title=f"Oferta ID {jid}",
                        external_job_id=str(jid),
                        match_decision="ELIGIBLE",
                        initial_state="ALREADY_APPLIED_PORTAL",
                        is_legacy_imported=True,
                    )
                    extra_history_ids += 1
        except Exception:
            pass

    repository.backup_to_workspace()
    return {
        "folders_scanned": len(folders),
        "imported_applications": imported_count,
        "confirmed_count": confirmed_count,
        "closed_count": closed_count,
        "pending_or_human_count": pending_count,
        "extra_history_ids_imported": extra_history_ids,
        "db_path": str(repository.db_path),
    }


if __name__ == "__main__":
    summary = migrate_legacy_workspace()
    print(json.dumps(summary, indent=2, ensure_ascii=False))
