# Changelog

## Unreleased
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
