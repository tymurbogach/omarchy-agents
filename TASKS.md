# TASKS — omarchy-agents

Fuente: auditoría 2026-09-29. Orden por riesgo. Marcar `[x]` al completar con verificación.

## Bloque 1 — Críticos lógica QML + status
- [x] `Main.qml:264-272` Fix colapso invertido: `normal/force` reemplaza a `limits` pendiente.
  Aceptación: `limits` pendiente + `normal` entrante = se ejecuta `normal`.
- [x] `Main.qml:207` Fix `NaN` timer: `isFinite` + fallback 900 + clamp 30..3600.
  Aceptación: setting basura no mata auto-refresh.
- [x] `Panel.qml:758-774` Fix status card: mismo campo para `visible` y `text` (o mostrar ambos).
  Aceptación: sin caja roja vacía, sin warning perdido.
- [x] `Panel.qml:941` Guarda `models.length` antes de `models[0]`.
Verificación: lectura de líneas citadas, `qmlscene` o reload shell sin errores.

## Bloque 2 — Concurrencia y churn QML
- [x] `Main.qml:112-116` Coalescing `recordsChanged` con `Qt.callLater` + batch.
- [x] `Main.qml:87-100` Batch `rebuildAgents` en arranque (evitar N recomputos).
- [x] `Main.qml:277-282,304-316` Watchdog para `updateProcess` y `customCollectorProcess`.
- [x] `Main.qml:126-143` Evitar inanición `limitsRetry.restart()` si flappea.
- [x] `Main.qml:695-705,782-821` Cancelar sync en vuelo al deshabilitar + guarda `syncConfigured()` en parse.
- [x] `Main.qml:452-465,471-536` `setProviderEnabled` sobre cola + preservar campos + mostrar `settingsWriteError` en panel.
- [x] `Agent.qml:20-22,25-32` Conservar último bueno en `onLoadFailed`, debounce, no reasignar si contenido igual, validar `id`.
- Extra: `orderRank` anti `__proto__` pollution.

## Bloque 3 — Sync aggregation
- [x] `Main.qml` `aggregateSnapshots`: dedupe por device (last-wins), `snapshotMs` + `syncFreshMs` 48h, `today*` solo si snapshot es de hoy, account-scope solo desde snapshots frescos.
- [x] `localSnapshot` incluye `updatedAtMs` para que los pares juzguen frescura.
- [x] `displayProvider` deviceCount propio del proveedor (nunca total del fleet), con guarda `isFinite`.
- [x] `providerHasData` incluye `todayTotalTokens`, `modelUsage` y `recentDays` (agentes billing-only ya no se ocultan).
Verificación: `tests/test_sync_aggregation.py` en Bloque 6 (harness JS→Python de la lógica pura).

## Bloque 4 — Colectores Python
- [x] `kimi.py` iteración cursor envuelta en `try sqlite3.Error` (conserva progreso parcial).
- [x] `opencode.py:limits_from_payload` + `limits_from_console_payload`: `isfinite`, `OverflowError`, valida `dict` en `fetch_*`.
- [x] `opencode.py:collect` unifica `windowless`: endpoint alcanzable sin ventanas conserva caché + `retry`.
- [x] `number()`: captura `OverflowError`. `local_day*`: `try` en `fromtimestamp`.
- [x] `clean_model_usage`/`clean_days`: validación profunda de caché (corrupta ya no crashea).
- [x] `kimi.py` wires en binario + `wire_identity` (inode/mtime/size); rotación/rewrite mismo tamaño fuerza rebuild.
- [x] `save_credential` preserva campos desconocidos. `configured_endpoints` exige `https://`.
- [x] `400 invalid_grant` = auth. `post_refresh_token` prueba todos los hosts; mixto auth+red = `network`. Sin token rancio en fallo red; `collect` conserva caché en `network`. Tier cacheado en `cache["tier"]`.
- [x] `database_identity` + `mtime`; `database_reusable` device/inode/shrink. (Nota: mtime-same-size NO fuerza rebuild en DB: appends caben en páginas asignadas; el reemplazo lo cubre `max(rowid) < lastRowId`.)
- [ ] DIFERIDO `collectors/_common.py`: extracción grande con riesgo de regresión y tests que importan módulos standalone. Hacerlo en PR separado con tests verdes.
Verificación: `python3 -m unittest discover -s tests` → 23 OK.

## Bloque 5 — Docs, manifest, higiene
- [x] `README.md:51-53` `bin/` → `collectors/` + referencia kimi. Bundled vs externos separados; twin policy documentada.
- [x] IPC: `show/hide` + nota async. Settings: `providers`/`providerOrder` JSON-only documentados; sync real (`On` + legados, expansiones, sanitizado, 48h/TTL).
- [x] Manifest schema sin cambios a propósito: el shell UI no edita objetos anidados; documentado en README en su lugar.
- [x] `.gitignore:6` `*.json.tmp` → `*.tmp` (casa con `mkstemp`).
- [x] `CHANGELOG.md` mínimo + purge combinado documentado.
- [x] `Panel.qml:windowTitle` fix `hours` y `1M context` vs `1m` (requiere contexto temporal).

## Bloque 6 — Tests faltantes
- [x] `tests/test_qml_logic.py`: extrae helpers puros del fuente real y los corre en node (`windowTitle`, `limitWindow`, `updateRank`, `mergeUpdateAgentIds`, `combineNumber`, `numberValue`).
- [x] `tests/test_collector_fixes.py`: `apply_fresh_limits` (4 casos), NaN/Inf, payload no-dict, `is_invalid_grant`, `is_https_url`, refresh multi-host (3 casos), caché corrupta, `database_reusable`, `save_credential` preserva extras + 0600, tier en caché.
- [x] `qmllint` no usable aquí (exit 255 también con el original: faltan imports Quickshell); sintaxis validada vía harness Node + revisión de diff.
- [x] `tests/test_collect_integration.py`: `collect()` end-to-end con FS aislado (`HOME`/`XDG_*`/`KIMI_CODE_HOME` en tmp) y red mockeada: wire→record→cache, límites+ tier de la API, supervivencia a outage con `retryAdvised`, api-key flow de opencode.
- [x] `main()` escribe el record en el state dir aislado; `purge --yes` borra solo sus ficheros y exige confirmación.
Verificación: `python3 -m unittest discover -s tests` → 55 OK.

## Bloque 7 — UX / a11y
- [x] `Accessible.*` en bar button, chips, `Meter` (ProgressBar + aviso), `LimitRow`, `DayRow`/`ModelRow` (exponen el texto del tooltip sin hover).
- [x] Hide con feedback: `hideProviderWithHint` deja aviso transitorio en el notice (mismo contrato que el aviso de default no elegible).
- [x] Drag cancelado en `onProvidersChanged`: un refresh mid-gesto ya no deja la marca de inserción clavada.
- [x] `formatTokenCount 999999→1.0M` (umbrales con medio paso), `formatMoney` JPY/KRW/VND sin decimales, `barThickness` + `BarGrow` compartidos.
- [x] Edge detection del popup `+N`: al abrir se acota `x` al borde izquierdo del panel (try/catch, caso normal intacto). Sin verificación visual en barra real: si el popup se comporta raro en tu pantalla, revertir este hunk.
- [x] Decisión `_common.py`: NO extraer. El shim de import dual que exigirían los tres contextos de ejecución (plugin runner como script, tests por spec, `--purge` manual) es peor que 150 líneas duplicadas de helpers estables y testeados.
Verificación: 55 tests OK.
