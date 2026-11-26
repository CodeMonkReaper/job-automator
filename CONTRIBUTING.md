# Guía de Contribución a Job Automator

¡Gracias por tu interés en contribuir! Este proyecto busca ofrecer una herramienta ética, auditable y modular para asistir postulaciones laborales sin falsos positivos ni alucinaciones.

## 1. Configuración del Entorno de Desarrollo

```bash
git clone <tu-fork-o-repo>
cd job-automator-oss
python -m venv .venv
# Windows (PowerShell): .\.venv\Scripts\Activate.ps1
# Linux / macOS:        source .venv/bin/activate

pip install -e ".[dev]"
playwright install chromium
python -m job_automator.cli init
```

Ejecuta la suite completa de pruebas antes y después de tus cambios:

```bash
python -m pytest tests -v
```

## 2. Cómo Agregar un Nuevo Portal de Empleo

1. **Adaptador de portal (`src/job_automator/adapters/<portal>.py`):**
   - Hereda de `BasePortalAdapter` (`adapters/base.py`).
   - Implementa extracción segura de ofertas, detección de sesión, llenado de formulario y retorno estructurado de `SubmissionOutcome`.
2. **Flujo interactivo y simulación (`src/job_automator/flows/<portal>_flow.py`):**
   - Hereda de `BasePortalFlow` (`flows/base_flow.py`).
   - Implementa `simulate()` (`DRY_RUN`) y `postulate_assisted()` (`Human-in-the-Loop`) respetando siempre `verify_pre_apply_offer_state()` y `verify_post_submit_confirmation()`.
3. **Registro en `flows/__init__.py`, `mcp_server.py` y `worker/runner.py`:**
   - Registra el adaptador y actualiza `PORTAL_READINESS` solo cuando existan pruebas unitarias y de verificación DOM sólidas.
4. **Guardias de seguridad (`browser/guards.py`):**
   - Agrega marcadores positivos explícitos y patrones de login/error específicos del portal en `guards.py`.

## 3. Reglas Estrictas de Higiene y Seguridad

- **Cero PII en el código o tests:** jamás incluyas nombres reales, teléfonos, correos personales, rutas locales (`C:\Users\...`) ni API keys en código, fixtures o commits. Toda lógica específica del candidato debe leerse desde `config/candidate_profile.json`.
- **Prohibido `taskkill` / cierre forzado de navegador:** usa exclusivamente conexión CDP no destructiva sobre Chrome.
- **Cero bypass anti-bot:** si un portal presenta CAPTCHA o autenticación reforzada, pausa o deriva a `NEEDS_HUMAN_REVIEW`.
- **Veracidad obligatoria:** todo generador de texto o CV debe validarse con `validate_text_truthfulness()`.

## 4. Checklist Antes de Abrir un Pull Request

- [ ] `python -m pytest tests -v` pasa al 100% (incluido `test_cli_and_oss_hygiene.py`).
- [ ] `python -m job_automator.cli doctor` y `evaluate` ejecutan sin errores.
- [ ] No se agregan archivos `.env`, `*.db`, `config/*.json` personales ni artefactos de `output/`.
- [ ] Documentación (`README.md` o docstrings) actualizada acorde al cambio.
