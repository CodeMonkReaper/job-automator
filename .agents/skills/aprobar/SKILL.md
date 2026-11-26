---
name: aprobar
description: Confirma y envía la postulación preparada en Chrome CDP, valida el comprobante oficial en el DOM, guarda captura y actualiza SQLite.
---

# /aprobar - Confirmar y Enviar la Postulación Actual

Al invocar `/aprobar`, el agente ejecuta este procedimiento:
1. Conectarse a la pestaña activa en Chrome vía CDP (`http://127.0.0.1:9222` o `JOB_AUTOMATOR_CDP_PORT`).
2. Localizar y pulsar el botón final de envío en el formulario previamente revisado por el usuario.
3. Verificar la confirmación oficial en el DOM con `verify_post_submit_confirmation()`, comprobando ausencia de errores de validación o redirecciones a login.
4. Capturar `confirmacion_postulacion.png` dentro de la carpeta del dossier en `output/` usando `safe_page_screenshot()`.
5. Transicionar la postulación en SQLite (`jobs.db`) al estado `CONFIRMED` con fecha y evidencia, y regenerar `output/APPLICATION_REPORT.md` mediante `build_consolidated_report()`.
