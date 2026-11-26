---
name: resumen
description: Consulta métricas consolidadas en SQLite y actualiza el informe Markdown output/APPLICATION_REPORT.md.
---

# /resumen - Informe y Métricas en Tiempo Real

Al invocar `/resumen`, el agente ejecuta este procedimiento:
1. Abrir la base de datos SQLite configurada (`JOB_AUTOMATOR_DB_PATH` o `data/jobs.db`).
2. Consultar conteos por estado (`CONFIRMED`, `SIMULATED_READY`, `NEEDS_HUMAN_REVIEW`, `SUBMITTED_UNVERIFIED`, `CLOSED_ON_PORTAL`, `ALREADY_APPLIED_PORTAL`, `SKIPPED_FILTER`) y distribución por portal.
3. Actualizar `output/APPLICATION_REPORT.md` mediante `build_consolidated_report(write_markdown=True)` (o `job-automator report`).
4. Presentar el resumen de métricas en el chat con enlace al informe actualizado.
