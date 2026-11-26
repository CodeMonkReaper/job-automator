"""Escritor transaccional del expediente documental por postulación."""

from __future__ import annotations

import json
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from job_automator.config import get_candidate_profile, get_logs_dir, get_output_dir
from job_automator.intelligence.answer_generator import format_clp


def clean_folder_name(company: str, title: str, app_id: int | None = None) -> str:
    c_str = (company or "").replace("…", "").strip(" .-–—")
    t_str = (title or "").replace("…", "").strip(" .-–—")
    if not c_str or c_str.lower() == t_str.lower():
        raw = f"Empresa - {t_str}"
    else:
        raw = f"{c_str} - {t_str}"
    cleaned = re.sub(r'[\\/*?:"<>|]', "", raw)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    base = cleaned[:85].strip(" .-–—…")
    if app_id is not None:
        return f"[ID {app_id}] {base}"
    return base


def prepare_dossier_files(
    company: str,
    title: str,
    portal: str,
    offer_url: str,
    external_job_id: str | None,
    location: str,
    salary_clp: int,
    src_docx: Path,
    src_pdf: Path,
    cv_target_name: str,
    cover_letter: str,
    answered_questions: dict[str, str],
    status_label: str = "En proceso de verificación",
    cv_selection_reason: str = "",
    workspace_root: Path | None = None,
    app_id: int | None = None,
) -> dict[str, Path]:
    """
    Prepara los archivos documentales del expediente en [ID] [Empresa] - [Cargo]/:
    1. CV_Candidate_[Cargo].docx
    2. CV_Candidate_[Cargo].pdf
    3. respuestas_postulacion.txt
    4. DETALLES_OFERTA.md
    """
    out_root = workspace_root or get_output_dir()
    folder_name = clean_folder_name(company, title, app_id=app_id)
    folder_path = out_root / folder_name
    folder_path.mkdir(parents=True, exist_ok=True)

    dst_docx = folder_path / f"{cv_target_name}.docx"
    dst_pdf = folder_path / f"{cv_target_name}.pdf"

    try:
        if src_docx.exists() and src_docx.resolve() != dst_docx.resolve():
            shutil.copy2(src_docx, dst_docx)
    except Exception:
        pass

    try:
        if src_pdf.exists() and src_pdf.resolve() != dst_pdf.resolve():
            shutil.copy2(src_pdf, dst_pdf)
    except Exception:
        pass

    sal_fmt = format_clp(salary_clp)

    # respuestas_postulacion.txt
    resp_lines = [
        f"POSTULACIÓN: {title} - {company}",
        f"PORTAL: {portal.capitalize()}",
        f"URL: {offer_url}",
        f"RENTA: {sal_fmt} líquidos mensuales (Disponibilidad Inmediata)",
        f"CV: {cv_target_name}.pdf",
        "",
        "--- PREGUNTAS Y RESPUESTAS ---",
    ]
    if answered_questions:
        for q_lbl, ans in answered_questions.items():
            resp_lines.append(f"\nPregunta: {q_lbl}\nRespuesta: {ans}")
    else:
        resp_lines.append("\nFormulario directo sin preguntas filtro adicionales.")

    resp_lines.extend(
        [
            "",
            "--- CARTA DE PRESENTACIÓN ---",
            cover_letter,
            "",
        ]
    )
    resp_path = folder_path / "respuestas_postulacion.txt"
    resp_path.write_text("\n".join(resp_lines), encoding="utf-8")

    prof = get_candidate_profile()
    edu = prof.get("education", {})
    companies = [c.get("name", "") for c in prof.get("authorized_companies", []) if c.get("name")]
    comp_txt = f" en {', '.join(companies)}" if companies else ""
    default_loc = prof.get("residence") or "Remote"

    # DETALLES_OFERTA.md
    detalles_md = (
        f"# Detalles de la Oferta: {title}\n\n"
        f"## 1. Información General\n"
        f"* **Empresa:** {company}\n"
        f"* **Cargo:** {title}\n"
        f"* **Portal:** {portal.capitalize()}\n"
        f"* **ID Oferta:** {external_job_id or 'N/A'}\n"
        f"* **Enlace Oficial:** {offer_url}\n"
        f"* **Ubicación:** {location or default_loc}\n"
        f"* **Renta Pretendida:** {sal_fmt} líquidos mensuales\n"
        f"* **Disponibilidad:** {prof.get('availability', 'Inmediata')}\n"
        f"* **Estado:** {status_label}\n\n"
        f"## 2. Requisitos y Perfil\n"
        f"* **CV Especializado:** `{cv_target_name}.pdf`\n"
        f"* **Motivo de Selección del CV:** {cv_selection_reason or 'Coincidencia con perfil del candidato'}\n"
        f"* **Acreditación del Postulante:** {edu.get('degree', 'Ingeniero en Informática')} ({edu.get('status', 'Titulado')}) en {edu.get('institution', 'Universidad')}, "
        f"con {prof.get('total_experience_years', 2)} años de experiencia profesional comprobable{comp_txt}.\n"
    )
    detalles_path = folder_path / "DETALLES_OFERTA.md"
    detalles_path.write_text(detalles_md, encoding="utf-8")

    return {
        "folder_path": folder_path,
        "docx_path": dst_docx,
        "pdf_path": dst_pdf,
        "respuestas_path": resp_path,
        "detalles_path": detalles_path,
    }


def append_submission_audit_log(
    *,
    session_id: str,
    application_id: int,
    portal: str,
    external_job_id: str | None,
    company: str,
    title: str,
    offer_url: str,
    final_url: str,
    status_code: str,
    reason: str,
    step_reached: str,
    selected_cv_base: str,
    selected_cv_pdf: str,
    cv_selection_reason: str,
    salary_clp: int,
    cover_letter: str,
    extracted_questions: list[str] | None = None,
    written_answers: dict[str, str] | None = None,
    dossier_dir: Path | None = None,
    form_screenshot: Path | None = None,
    final_screenshot: Path | None = None,
    workspace_root: Path | None = None,
) -> dict[str, Path]:
    """
    Registra un log completo y auditable de todo lo escrito, enviado, respondido y diagnosticado
    tanto en JSONL (data/logs/envios_detallados.jsonl) como en Markdown
    (APPLICATION_AUDIT_LOG.md en el directorio de salida).
    """
    now_iso = datetime.now(timezone.utc).isoformat()
    logs_dir = get_logs_dir()
    logs_dir.mkdir(parents=True, exist_ok=True)
    out_root = workspace_root or get_output_dir()
    out_root.mkdir(parents=True, exist_ok=True)
    jsonl_path = logs_dir / "envios_detallados.jsonl"
    md_path = out_root / "APPLICATION_AUDIT_LOG.md"

    network_receipt: dict[str, Any] = {}
    if dossier_dir:
        rec_file = dossier_dir / "comprobante_envio_red.json"
        if rec_file.exists():
            try:
                network_receipt = json.loads(rec_file.read_text(encoding="utf-8"))
            except Exception:
                network_receipt = {}

    record = {
        "timestamp_utc": now_iso,
        "session_id": session_id,
        "application_id": application_id,
        "portal": portal,
        "external_job_id": external_job_id,
        "company": company,
        "title": title,
        "offer_url": offer_url,
        "final_url": final_url,
        "status_code": status_code,
        "step_reached": step_reached,
        "reason": reason,
        "cv": {
            "selected_cv_base": selected_cv_base,
            "selected_cv_pdf": selected_cv_pdf,
            "selection_reason": cv_selection_reason,
            "dom_attached_filename": network_receipt.get("cv_audit", {}).get("dom_attached_filename"),
            "dom_attachment_verified": network_receipt.get("cv_audit", {}).get("dom_attachment_verified"),
            "sha256_checksum": network_receipt.get("cv_audit", {}).get("sha256_checksum"),
        },
        "network": network_receipt.get("network_audit", {}),
        "transmission_verdict": network_receipt.get("transmission_verdict"),
        "salary_clp": salary_clp,
        "cover_letter_written": cover_letter,
        "extracted_questions": extracted_questions or [],
        "written_answers": written_answers or {},
        "screenshots": {
            "form_screenshot": str(form_screenshot) if form_screenshot else None,
            "final_screenshot": str(final_screenshot) if final_screenshot else None,
        },
    }

    with open(jsonl_path, "a", encoding="utf-8") as jf:
        jf.write(json.dumps(record, ensure_ascii=False) + "\n")

    if not md_path.exists():
        md_header = (
            "# 📋 Log de Auditoría Detallada de Envíos, CVs y Respuestas Escritas\n\n"
            "Este archivo registra automáticamente en tiempo real **cada postulación procesada**, incluyendo:\n"
            "- El archivo CV exacto seleccionado, su motivo y su verificación en el DOM y en red.\n"
            "- La renta y carta de presentación exacta escrita en el formulario.\n"
            "- Todas las preguntas filtro extraídas y las respuestas exactas enviadas.\n"
            "- El diagnóstico técnico de confirmación o error para mejora continua.\n\n"
            "---\n\n"
        )
        md_path.write_text(md_header, encoding="utf-8")

    dom_ver = record["cv"]["dom_attachment_verified"]
    post_url = record["network"].get("post_request_url") or "N/A"
    is_mp = record["network"].get("is_multipart_form_data")
    verdict = record["transmission_verdict"] or "N/A"

    q_block = ""
    if written_answers and not extracted_questions:
        q_lines = [f"  - **Pregunta:** `{k}`\n    - **Respuesta escrita:** {v}" for k, v in written_answers.items()]
        q_block = "\n".join(q_lines)
    elif extracted_questions:
        q_lines = [f"  - **Pregunta extraída (pendiente IA):** `{q}`" for q in extracted_questions]
        q_block = "\n".join(q_lines)
    else:
        q_block = "  - *(Sin preguntas adicionales en el formulario)*"

    entry_md = (
        f"## [{now_iso[:19]} UTC] Oferta #{application_id} — {title} ({company})\n"
        f"- **Portal:** `{portal}` (ID Externo: `{external_job_id or 'N/A'}`) | **Sesión:** `{session_id}`\n"
        f"- **Estado Resultante:** **`{status_code}`** (Paso: `{step_reached}`)\n"
        f"- **Diagnóstico / Motivo:** {reason}\n"
        f"- **URL Oferta:** {offer_url}\n"
        f"- **URL Final:** {final_url}\n"
        f"- **CV Seleccionado:** `{selected_cv_pdf}` *(Base: `{selected_cv_base}`)*\n"
        f"  - **Razón de selección CV:** {cv_selection_reason}\n"
        f"  - **Verificado en DOM (`input[type=file]` / Radio):** `{dom_ver}` (`{record['cv']['dom_attached_filename']}`)\n"
        f"  - **Veredicto de Red:** `{verdict}` | **POST Endpoint:** `{post_url}` | **Multipart:** `{is_mp}`\n"
        f"- **Renta Escrita:** `{format_clp(salary_clp)}` líquidos\n"
        f"- **Preguntas y Respuestas Escritas:**\n{q_block}\n"
        f"- **Carta de Presentación Escrita:**\n"
        f"  ```text\n  {cover_letter.replace(chr(10), chr(10) + '  ')}\n  ```\n\n"
        f"---\n\n"
    )
    with open(md_path, "a", encoding="utf-8") as mf:
        mf.write(entry_md)

    return {"jsonl_path": jsonl_path, "md_path": md_path}

