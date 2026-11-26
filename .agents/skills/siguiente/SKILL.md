---
name: siguiente
description: Busca la siguiente vacante compatible no postulada, compila un CV ATS de 1 página con Playwright, sincroniza/adjunta en Chrome CDP y redacta respuestas veraces para revisión humana.
---

# /siguiente - Buscar y Preparar la Próxima Oferta

Al invocar `/siguiente`, el agente ejecuta este procedimiento:
1. Consultar la base SQLite configurada (`JOB_AUTOMATOR_DB_PATH` o `data/jobs.db`) para obtener la próxima oferta elegible que no esté en `CONFIRMED`, `SKIPPED_FILTER`, `CLOSED_ON_PORTAL` ni `ALREADY_APPLIED_PORTAL`.
2. Descartar ofertas ajenas al perfil definido en `config/search_preferences.json` usando `evaluate_offer()`.
3. Generar el CV personalizado ATS de 1 página mediante `job_automator.intelligence.ats_cv_generator`:
   - Guardar el PDF en `output/[ID <num>] <Empresa> - <Cargo>/CV_<Candidato>_ATS_ID<num>.pdf`.
   - Validar con `validate_text_truthfulness()` contra `config/candidate_profile.json`.
   - Detener con `ERROR_CV_GENERATION` si falla la compilación o la validación.
4. Conectar a Chrome vía CDP (`JOB_AUTOMATOR_CDP_PORT`, por defecto `9222`), sincronizar o adjuntar el CV PDF según el portal y pre-rellenar campos estándar y preguntas filtro.
5. Presentar en el chat el resumen de la oferta (`Empresa`, `Cargo`, `URL`, `Score ATS`, `Palabras clave`, enlace `file:///` al PDF generado y respuestas propuestas) solicitando confirmación explícita (`/aprobar` o `/descartar`).
