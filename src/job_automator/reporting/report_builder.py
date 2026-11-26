"""Generador del informe consolidado de postulaciones desde SQLite."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

from job_automator.config import get_candidate_profile, get_output_dir
from job_automator.db.repository import JobRepository
from job_automator.intelligence.answer_generator import format_clp


def build_consolidated_report(
    repo: JobRepository | None = None,
    session_id: str | None = None,
    write_markdown: bool = True,
    workspace_root: Path | None = None,
    report_filename: str = "APPLICATION_REPORT.md",
) -> dict[str, Any]:
    """Genera el resumen ejecutivo de métricas y actualiza el informe Markdown si se solicita."""
    repository = repo or JobRepository()
    counts_global = repository.get_counts_by_state(include_legacy=True)
    counts_session = repository.get_counts_by_state(session_id=session_id, include_legacy=False) if session_id else {}
    portals_dist = repository.get_counts_by_portal(confirmed_only=False)

    total_processed = sum(counts_global.values())
    confirmed_total = counts_global.get("CONFIRMED", 0)
    closed_total = counts_global.get("CLOSED_ON_PORTAL", 0)
    unverified_total = counts_global.get("SUBMITTED_UNVERIFIED", 0)
    human_review_total = counts_global.get("NEEDS_HUMAN_REVIEW", 0)
    simulated_total = counts_global.get("SIMULATED_READY", 0)
    failed_total = (
        counts_global.get("FAILED_VALIDATION", 0)
        + counts_global.get("FAILED_RETRYABLE", 0)
        + counts_global.get("FAILED_FATAL", 0)
    )

    recent_apps = repository.list_applications(
        session_id=session_id,
        include_legacy=(session_id is None),
        limit=50,
    )

    out_root = workspace_root or get_output_dir()
    out_root.mkdir(parents=True, exist_ok=True)
    report_path = out_root / report_filename

    if write_markdown and not session_id:
        prof = get_candidate_profile()
        edu = prof.get("education", {})
        companies = [c.get("name", "") for c in prof.get("authorized_companies", []) if c.get("name")]
        companies_txt = f" ({', '.join(companies)})" if companies else ""
        all_apps = repository.list_applications(include_legacy=True, limit=1000)

        md_lines = [
            f"# Informe Consolidado de Postulaciones Laborales - {prof.get('full_name', 'Candidato')}",
            "",
            f"**Fecha de Actualización:** {datetime.now().strftime('%Y-%m-%d %H:%M')}  ",
            f"**Candidato:** {prof.get('full_name', 'Candidato')}  ",
            f"**Título:** {edu.get('degree', 'Ingeniero en Informática')} ({edu.get('institution', 'Universidad')}, {edu.get('status', 'Titulado')})  ",
            f"**Experiencia:** {prof.get('total_experience_years', 2)} años demostrables{companies_txt}  ",
            f"**Contacto:** `{prof.get('phone', '')}` | `{prof.get('email', '')}` | {prof.get('residence', 'Remote')}  ",
            f"**Disponibilidad:** {prof.get('availability', 'Inmediata')}  ",
            "",
            "---",
            "",
            "## 1. Resumen Ejecutivo de Métricas Verificadas (SQLite)",
            "",
            "| Métrica Clave | Total | Detalle de Verificación |",
            "| :--- | :---: | :--- |",
            f"| **Total Postulaciones Registradas** | **{total_processed}** | Trazabilidad completa en base de datos `jobs.db` |",
            f"| **Postulaciones Confirmadas Oficialmente** | **{confirmed_total}** | Confirmación del portal verificada en DOM + captura |",
            f"| **Convocatorias Cerradas en Portal** | **{closed_total}** | Ofertas expiradas o cerradas documentadas |",
            f"| **Pendientes de Verificación / Intervención** | **{unverified_total + human_review_total}** | CAPTCHAs, formularios externos o confirmación pendiente |",
            f"| **En Modo Simulación (`DRY_RUN`)** | **{simulated_total}** | Listas para aprobación de envío |",
            "",
            "### Distribución por Portal",
            "",
            "| Portal de Empleo | N° Postulaciones |",
            "| :--- | :---: |",
        ]
        for p_name, cnt in portals_dist.items():
            md_lines.append(f"| **{p_name.capitalize()}** | {cnt} |")

        md_lines.extend(
            [
                "",
                "---",
                "",
                "## 2. Registro Consolidado de Postulaciones",
                "",
                "| # | Empresa | Cargo | Portal | Renta Pretendida | Estado Verificado |",
                "| :-: | :--- | :--- | :---: | :---: | :---: |",
            ]
        )

        state_badges = {
            "CONFIRMED": "✅ Confirmada",
            "SIMULATED_READY": "🧪 Simulada (Lista)",
            "SUBMITTED_UNVERIFIED": "⏳ Pendiente Verificación",
            "CLOSED_ON_PORTAL": "🔒 Cerrada en Portal",
            "ALREADY_APPLIED_PORTAL": "ℹ️ Ya Postulada Antes",
            "NEEDS_HUMAN_REVIEW": "👤 Requiere Intervención",
            "FAILED_VALIDATION": "❌ Error Validación",
            "FAILED_RETRYABLE": "⚠️ Fallo Reintentable",
            "FAILED_FATAL": "❌ Fallo Definitivo",
        }

        for idx, app in enumerate(all_apps, start=1):
            f_path_str = (app.get("folder_path") or "").replace("\\", "/")
            cargo_link = f"[{app['title']}](file:///{f_path_str})" if f_path_str else app["title"]
            sal_str = format_clp(int(app.get("expected_salary_clp") or 800000))
            badge = state_badges.get(app["state"], app["state"])
            md_lines.append(
                f"| {idx} | **{app['company_name']}** | {cargo_link} | {app['portal'].capitalize()} | **{sal_str} líquidos** | {badge} |"
            )

        report_path.write_text("\n".join(md_lines) + "\n", encoding="utf-8")

    return {
        "session_id": session_id,
        "global_counts_by_state": counts_global,
        "session_counts_by_state": counts_session,
        "portals_distribution": portals_dist,
        "summary": {
            "total_registered": total_processed,
            "confirmed": confirmed_total,
            "closed_on_portal": closed_total,
            "submitted_unverified": unverified_total,
            "needs_human_review": human_review_total,
            "simulated_ready": simulated_total,
            "failed": failed_total,
        },
        "recent_applications": [
            {
                "id": a["id"],
                "company": a["company_name"],
                "title": a["title"],
                "portal": a["portal"],
                "state": a["state"],
                "salary_clp": a["expected_salary_clp"],
                "folder_name": a["folder_name"],
            }
            for a in recent_apps
        ],
        "report_path": str(report_path),
    }
