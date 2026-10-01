# Changelog

## Unreleased
- `collectors/codex.py`: Codex now uses a bundled retry-aware wrapper. It
  preserves Omarchy local statistics, retries transient RPC startup failures
  three times, writes records atomically, and requests one 30-second retry.
- `Main.qml`: when the bundled Codex collector exists, the Omarchy update
  command excludes Codex so it cannot overwrite the retry-aware record.
- `Agent.qml`: usage records are validated against the record contract at the
  border (object with non-empty `id`, `schemaVersion` 1 when present, `limits`
  as a list when present); contract breakers are rejected with a warning
  instead of rendering as stale v1 data.
- `Main.qml`: `authHelpTextForRecord` drops the stale first-party "run login"
  hint when a record carries limits and reports no problem (TEMPORARY pending
  the upstream collector fix, review 2027-01; see
  `docs/upstream-codex-stale-authhelp.md`). New pure `recordUpdatedMs`
  helper; `displayProvider` exposes it.
- `Panel.qml`: corrupt limit percents (`NaN`/`Infinity`) no longer pass the
  `limitWindows` filter. New pure `recordAgeText` helper; the footer shows
  `Updated Xm ago` when a record is older than one refresh interval plus
  margin, so a stale zero reads as stale.
- `Panel.qml`: tabs hint shortened to one static line below the chips
  (`Double-click: default • Right-click: hide`).
- Tests: `test_qml_logic` extractor skips comments and quoted strings; golden
  Codex/Claude record cases, `recordUpdatedMs`/`recordAgeText` cases, and the
  infinite-percent guard. New `tests/test_sync_aggregation.py` covering
  per-device last-wins dedupe, the 48h freshness window, today-only-from-today,
  and account-scope max merge (the suite `TASKS.md` promised).

## 1.2.2
- Publish the Codex retry collector and its update integration.

## 1.2.0
- Review pass: `find` discovery processes warn on failure; `collectorId`
  rejects non-`.py`; empty update scopes normalize to full runs;
  `numberValue` clamps negatives; balance currency truncated; `expandPath`
  handles `$HOME`/`${HOME}` bare forms.
- `Main.qml`: queue collapse prefers the fuller update kind and merges agent
  scopes; `refreshIntervalSec` falls back to 900 on garbage and clamps to
  30–3600; `recordsChanged`/`rebuildAgents` batch via `callLater`; 120s
  watchdog aborts hung collectors; `limitsRetry` no longer starves on flapping
  records; disabling sync kills in-flight scans and stale scans cannot
  repopulate data; `setProviderEnabled` preserves unknown per-provider fields
  and builds on the queued tail; `orderRank` ignores prototype keys.
- `Main.qml` sync: snapshots dedupe per device (last-wins), carry
  `updatedAtMs`, older than 48h no longer move today's counters or win
  account-scope merges; provider device count no longer falls back to the
  fleet total; token-only providers count as data.
- `Panel.qml`: status card shows when either status or help text exists and
  renders both; model share guards an empty list; save errors surface in the
  footer; `windowTitle` only treats a duration as a session when the label
  talks about time, and accepts `hours`.
- `Agent.qml`: keeps the last good record on transient read errors, debounces
  reloads, validates `id`, skips identical content.
- `collectors/kimi.py`: binary wire reads with inode/mtime/size rotation
  detection; SQLite cursor wrapped mid-scan; `400 invalid_grant` is auth; all
  OAuth hosts tried before reporting auth; no stale-token probe after refresh
  network failure; cached limits survive network outages; membership tier
  cached; credential rotation preserves unknown fields; managed hosts must be
  `https://`; deep cache validation.
- `collectors/opencode.py`: `isfinite` guards on limits, non-dict payloads
  rejected, windowless endpoints keep the cache with retry, deep cache
  validation, `OverflowError` handled.
- Docs: bundled vs external collectors separated, `bin/` reference fixed,
  asset twin policy documented, IPC `show`/`hide` documented, settings table
  notes JSON-only keys and real sync semantics, combined purge command.
- `.gitignore`: `*.tmp` matches the real `mkstemp` names.
