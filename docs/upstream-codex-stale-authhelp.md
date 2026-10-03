# Upstream: stale `authHelpText` on successful limits fetch

For `omarchy-agent-usage-codex` (and the same pattern in
`omarchy-agent-usage-claude`). Do NOT file from a script; file by hand and
replace the link below with the real issue URL.

## Reproduction

```bash
omarchy-agent-usage-codex --limits-only | python3 -c \
  "import json,sys; d=json.load(sys.stdin); print({k: d.get(k) for k in ['limits','tierLabel','usageStatusText','authHelpText']})"
```

Observed 2026-10-01 with a valid Codex login (`~/.codex/auth.json` fresh,
plan Plus):

- `limits`: two fresh windows (`5h window` 0%, `Weekly (7-day)` 45%)
- `tierLabel`: `plus`
- `usageStatusText`: `""`
- `authHelpText`: `"Run `codex login` to authenticate."`  <-- stale

## Root cause

`fetch_codex_rpc()` initializes the result with
`authHelpText = AUTH_HELP` and only overwrites it on the failure paths. The
success path sets `tierLabel` and appends `limits` but never clears the hint,
so every healthy record carries a login warning and the panel shows an auth
card for an authenticated user.

`collect_limits()` in `omarchy-agent-usage-claude` has the same shape:
`result` starts with `authHelpText = AUTH_HELP` and the `probe["ok"]` path
returns without clearing it.

## Proposed fix (upstream)

Clear `authHelpText` (and keep `usageStatusText` empty) on every path that
returns live limits:

```python
# codex fetch_codex_rpc(), after the window loop:
result["authHelpText"] = ""
```

```python
# claude collect_limits(), inside `if probe["ok"]:`:
result["authHelpText"] = ""
```

Both files already set a specific message on every genuine failure path
(`"Codex limits unavailable"`, `"Sign-in expired"`, `"Waiting for auth"`),
so clearing on success loses no signal.

## Local workaround (this repo)

`Main.qml: authHelpTextForRecord()` drops the hint when the record carries
limits and reports no problem. Marked TEMPORARY, review 2027-01. Remove it
once the upstream collectors clear the hint on success.

Issue: not filed upstream yet. File it by hand and link it here.
