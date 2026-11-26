# Política de Seguridad y Uso Responsable

## Principios Inviolables de Seguridad

1. **Respeto a los procesos del usuario:** el sistema se conecta a una sesión existente del navegador mediante Chrome DevTools Protocol (CDP) y jamás ejecuta `taskkill`, `pkill` ni cierre forzado sobre instancias de Chrome.
2. **Prohibición de bypass de controles:** Job Automator no elude CAPTCHAs, verificaciones anti-bot ni pantallas de autenticación. Cuando detecta una barrera de seguridad o redirección externa, detiene el envío automático y marca la postulación como `NEEDS_HUMAN_REVIEW`.
3. **Protección de credenciales y datos personales:**
   - Las claves de API (como `GEMINI_API_KEY`) se cargan únicamente mediante variables de entorno o `.env` local (excluido por `.gitignore`).
   - Los logs JSONL sanitizan automáticamente contraseñas, cookies, tokens y URLs de depuración CDP (`logging_utils.py`).
   - La configuración personal del candidato vive en `config/*.json` y está excluida del control de versiones.
4. **Validación determinista contra alucinaciones:** toda respuesta o CV generado pasa por `validate_text_truthfulness()` antes de inyectarse en un formulario o guardarse como PDF.
5. **Control humano (*Human-in-the-Loop*):** los flujos asistidos requieren revisión y confirmación explícita del usuario antes del envío final y exigen comprobación positiva en el DOM para registrar `CONFIRMED`.

## Reporte de Vulnerabilidades

Si encuentras una falla de seguridad (exposición de tokens en logs, fuga de rutas locales o errores de validación que puedan causar envíos duplicados):

1. No publiques la vulnerabilidad en un issue público con datos sensibles o exploits reproducibles.
2. Abre un reporte privado de seguridad (GitHub Security Advisory) describiendo el módulo afectado, los pasos de reproducción y el impacto esperado.
3. Incluye una propuesta de prueba de regresión si es posible.
