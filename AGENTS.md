# Reglas de Operación — Job Automator (Open Source)

Este repositorio implementa un motor modular en Python + Playwright CDP + FastMCP + SQLite para asistir postulaciones laborales con supervisión humana (*Human-in-the-Loop*).

## 1. Comandos de Operación Asistida

- **`/siguiente`** o `prepare_offer_for_review(app_id)`: evalúa la vacante, compila un CV ATS de 1 página, pre-rellena el formulario en Chrome CDP y presenta la ficha para revisión humana.
- **`/aprobar`** o `confirm_application_submission(app_id)`: confirma el envío tras el visto bueno explícito del usuario, valida el DOM de confirmación, guarda captura y registra `CONFIRMED` en SQLite.
- **`/descartar`**: marca la oferta actual como `SKIPPED_FILTER` y pasa a la siguiente.
- **`/resumen`** o `generate_report(update_markdown_file=True)`: consulta métricas en SQLite y actualiza `output/APPLICATION_REPORT.md`.
- **Sesiones en lote (MCP):** `start_job_session(mode="DRY_RUN"|"LIVE")`, `get_session_status()`, `pause_session()`, `resume_session()`, `stop_session()`, `retry_failed_tasks()`.

## 2. Guardias de Seguridad y Confiabilidad

1. **Prohibición absoluta de `taskkill`**: jamás cerrar o matar procesos de Chrome del usuario; operar siempre conectando vía CDP a una sesión iniciada por la persona.
2. **Pre-Flight Check obligatorio**: no procesar formularios cuando la sesión del portal en Chrome no esté autenticada.
3. **Cero alucinación**: toda respuesta y CV generado debe derivarse exclusivamente de `config/candidate_profile.json` y aprobar `validate_text_truthfulness()`.
4. **Cero falsos positivos**: marcar `CONFIRMED` solo cuando `verify_post_submit_confirmation()` confirme un marcador positivo explícito en el DOM sin alertas de error ni pantallas de login.
5. **Supervisión humana**: toda postulación requiere revisión explícita de la persona antes del envío definitivo; nunca resolver ni eludir CAPTCHAs ni bloqueos anti-bot.
