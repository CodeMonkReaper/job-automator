---
name: descartar
description: Descarta la oferta laboral actual marcándola como SKIPPED_FILTER en SQLite y avanza a la siguiente vacante elegible.
---

# /descartar - Descartar Oferta y Avanzar

Al invocar `/descartar`, el agente ejecuta este procedimiento:
1. Identificar la oferta actual abierta en el navegador o pendiente de revisión en el chat.
2. Registrar el cambio de estado en SQLite (`jobs.db`) a `SKIPPED_FILTER` con motivo explícito.
3. Cerrar o apartar la pestaña de la oferta descartada sin terminar el proceso `chrome.exe`.
4. Invocar el flujo de `/siguiente` para preparar la próxima vacante disponible.
