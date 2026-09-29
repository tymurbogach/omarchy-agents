# My Agents

One bar icon and one panel for every AI coding subscription on the machine.
The panel is strictly a display: it watches the usage records that
`omarchy-agent-usage-update` writes to `~/.local/state/omarchy/agents/usage/`
and draws whatever appears there. `Panel.qml` owns the bar button and the
popup; `Main.qml` discovers and watches the records (and handles the optional
cross-device aggregation); `Agent.qml` is the per-record file watcher.

## Panel

- **Hero** — the mark, the tool, and the plan it runs on ("Max 20x", "Pro").
  Auth and endpoint problems replace the plan line and repeat in a card.
- **Subscription switch** — one chip per enabled agent (`h`/`l` or click).
  It appears only when more than one agent is enabled.
- **Limits** — the percentage of each allowance used, a matching meter, and
  the time until the session or weekly window resets.
- **Balance** — prepaid agents report a credit ledger instead of limits:
  remaining credit, a fuel-gauge meter that drains toward empty, and
  funded-versus-spent detail.
- **Tokens by day** — one row per day for the last week: day, bar, tokens, with today
  bolded at the bottom. Hover today for its prompt and session count.
- **Tokens by model** — tokens per model with the bar behind each row scaled
  to the heaviest model,
  the same way the weekly chart scales to its busiest day. Hover for the
  input / output / cache split.

A subscription appears only when it is enabled in settings and has actually
recorded usage — on this machine or on a synced one. With one such agent
there is no switch row at all; with none, the module leaves the bar entirely
rather than sitting there with nothing to say. A CLI installed mid-session
shows up at the next refresh, so nothing polls the disk waiting for it.

That self-hiding is why the widget ships in the default bar layout: a machine
that has never run an AI coding agent draws nothing, and the icon arrives on
its own the first time a scan finds usage. Drop it with
`omarchy plugin disable omarchy.agents`.

## Data

Each agent is one JSON record in `~/.local/state/omarchy/agents/usage/`,
written by `omarchy-agent-usage-update`. That command runs one
`omarchy-agent-usage-<agent>` collector per agent; the widget invokes it
on its refresh timer and whenever you ask for a refresh, and picks up any
record that lands in the directory regardless of who wrote it.

This plugin also runs its bundled collectors from `collectors/`. A new bundled
collector needs no QML change: its filename is its provider ID and it writes
the same JSON contract.

Adding an agent therefore never touches this plugin: ship a collector that
prints the record contract (see the `claude` and `codex` collectors in
`bin/`), and the panel gains a tab. An `assets/<id>.svg` mark is optional —
with an `assets/<id>-light.svg` twin if the mark needs a dark variant for
light surfaces — and the bar glyph stands in when there is none.

| Collector | Limits | Local stats |
|---|---|---|
| `claude` | Anthropic's OAuth usage endpoint (5-hour session + 7-day weekly) | `~/.claude/projects` transcripts, opencode sessions on an Anthropic provider, plus `stats-cache.json` and `history.jsonl` as fallback |
| `codex` | The Codex app-server RPC | native Codex CLI session files (plus pi and opencode sessions) |
| `fireworks` | Estimated prepaid balance: configured funding minus rated account costs | Fireworks billing API, grouped by day and model for the last 30 days |
| `kimi` | Kimi Code membership quota (rolling 5-hour + 7-day windows) via the OAuth login | Kimi Code native session wire logs, plus OpenCode rows on a Kimi provider |
| `opencode` | OpenCode Go rolling, weekly, and monthly allowances when connected | OpenCode messages from Zen and Go |

## OpenCode Go

Requirements: Python 3, OpenCode, and an active OpenCode Go subscription.

The OpenCode collector reads `~/.local/share/opencode/opencode.db` in SQLite
read-only mode. It counts messages with `providerID: "opencode"` and
`providerID: "opencode-go"`. This shows OpenCode use even when the active model
comes from Zen. It does not count messages from unrelated providers.

For the browser login, OpenCode stores the active OAuth account in
`~/.local/share/opencode/opencode.db`. The collector reads its access token and
active organization ID in read-only mode, then sends them only to
`https://opencode.ai/console/api/go/status`. It never writes either value to
the panel record, cache, or logs.

The collector also supports the API-key flow from
`~/.local/share/opencode/auth.json` as a fallback. The browser account path is
the normal path when `/connect` shows a six-digit code.

When the browser account exists, the panel shows its rolling, weekly, and
monthly limits. If no account or key is available, the panel still shows local
OpenCode statistics without displaying a false authentication error. If the
OAuth token expires, complete the OpenCode browser login again.

The first local scan builds a cache under
`$XDG_CACHE_HOME/omarchy-agents/opencode-go-v1.json`. Later scans process only
new SQLite rows. A forced refresh or a replaced database rebuilds the cache.

## Kimi Code

Requirements: Python 3, Kimi Code, and an active Kimi membership login
(`kimi /login`).

The Kimi collector reads `~/.kimi-code/sessions/` wire logs in read-only
mode. It counts user turns as prompts and per-turn token usage by model. It
also counts OpenCode rows with `providerID` `kimi-for-coding` or `moonshot`,
which no other collector claims. This shows Kimi use even when the model runs
inside Codex, Claude Code, or another third-party tool.

For limits, the collector refreshes the stored OAuth credential when it is
close to expiry, then asks the coding API usage endpoint. A rotated
credential is written back to the CLI's credentials file the same way the
CLI writes it (atomic write, mode 0600, kept only if no newer rotation
landed meanwhile). Credentials and their values never reach the panel
record, cache, or logs. The panel shows the enforced weekly pool plus the
rolling 5-hour window, with the membership tier from the account profile.
If the login expires, run `/login` in Kimi Code again. Without a login the
panel still shows local Kimi statistics with no false authentication error.

The first local scan builds a cache under
`$XDG_CACHE_HOME/omarchy-agents/kimi-v1.json`. Later scans process only
appended wire lines and new database rows. A forced refresh rebuilds it.

Before removal, run this command from the installed plugin directory:

```bash
python3 collectors/kimi.py --purge --yes
```

The command removes only the Kimi cache and the Kimi usage record.

## Install

This repository replaces the existing local clone with the same plugin ID.
Copy the current plugin to this repository before you remove it. Then disable
and remove `cyberdyne.agents`, add this repository with `omarchy plugin add`,
and enable it in the previous bar section. Omarchy owns the installed copy in
`~/.config/omarchy/plugins/cyberdyne.agents/`.

Run `omarchy restart shell` after installation if the shell does not reload the
new collector list. Do not modify `/usr/share/omarchy/`.

## Remove

Before removal, run this command from the installed plugin directory:

```bash
python3 collectors/opencode.py --purge --yes
```

The command removes only the OpenCode cache and the OpenCode usage record.
Then disable and remove `cyberdyne.agents` with the Omarchy plugin commands.

Claude limits need a signed-in CLI; without credentials the panel says so and
falls back to local stats only. A non-default Claude directory is honored via
`CLAUDE_CONFIG_DIR`, Codex via `CODEX_HOME`. Fireworks reads
`FIREWORKS_API_KEY` and `FIREWORKS_ACCOUNT_ID` first, then
`~/.fireworks/auth.ini` (which `firectl set-api-key` creates), then the key
opencode stores in `~/.local/share/opencode/auth.json` when Fireworks is
signed in there.

### Fireworks balance

The collector first asks the account's `:getBalance` endpoint for the real
prepaid ledger. That endpoint exists but is permission-gated, and as of
August 2026 no console-issued API key passes it — Fireworks appears to
reserve it for the dashboard session. The probe stays because it is cheap
and the live figure lights up automatically if Fireworks ever opens it to
keys. Until then the collector falls back to estimating the balance from
configuration in `~/.config/omarchy/agents/fireworks.json`:

```json
{
  "accountId": "",
  "fundedAmount": 20,
  "fundedAt": "2026-07-01"
}
```

Set `fundedAmount` to the credits purchased and optionally `fundedAt` to the
purchase date; with no date, the collector uses the account creation time. It
subtracts rated account costs and the panel labels the result as estimated.
For a later top-up, increase `fundedAmount` by the new credit while keeping
the original `fundedAt`, so both the funding and spend still cover the same
period. `accountId` only matters when one API key can access several
accounts. Without a configured `fundedAmount` the tab still shows token
usage, just no balance. With a live ledger, `fundedAmount` is optional and
only adds the meter and the spent-of-funded line under the real figure.

## Interactions

- Bar icon: left = panel, right = launch agent, middle = next subscription.
- Panel: `h`/`l` switch subscription, `Shift+H`/`Shift+L` move it,
  `x` hides it, `j`/`k` scroll, `r` or Enter refresh,
  Tab moves to the neighboring bar panel, Esc closes.
- Chips: drag sideways to reorder, right-click hides.
  Double-click a Claude, Codex, or OpenCode tab to make it the default
  agent Omarchy launches. Double-clicking Kimi or Fireworks answers in
  the notice instead: they have no Omarchy agent.
  The default agent's chip wears a dot underneath, and a notice above
  the tabs names it.
- The `+N` next to the title lists hidden subscriptions to restore;
  hiding also stops their scans. The switch row needs two visible agents,
  so the last one cannot hide itself out of the bar.
- IPC: `omarchy-shell omarchy.agents <open|close|toggle|refresh|next>`.

## Settings

Settings live in the widget's entry in `~/.config/omarchy/shell.json`. The
top-level keys can be set with
`omarchy bar set omarchy.agents <key> <value>`:

| Key | Default | What it does |
|---|---|---|
| `providerOrder` | `{}` | Subscription tab order, as `{id: position}`; new agents append alphabetically. The panel rewrites it when you drag, use `Shift+H`/`Shift+L`, or the chip menu. (An object, not an array: the shell IPC layer flattens array arguments.) |
| `refreshIntervalSec` | `900` | How often the usage records regenerate |
| `syncMode` | `"Off"` | `"On"` writes this machine's snapshot and merges the others |
| `syncDir` | `""` | A folder synced by Syncthing, Dropbox, rsync, … |
| `syncFileName` | `<hostname>.json` | This machine's snapshot file |
| `syncDeviceId` | hostname | Stable device name inside the snapshot |

Numbers need `--json`, or they land in `shell.json` as strings:

```bash
omarchy bar set omarchy.agents refreshIntervalSec 300 --json
omarchy bar set omarchy.agents syncDir '~/Sync/agent-usage'
```

Per-agent enablement is nested, and `set` writes its key literally rather
than walking a dotted path — so pass the whole `providers` object as JSON (or
edit `shell.json` directly):

```bash
omarchy bar set omarchy.agents providers '{
  "claude": { "enabled": true },
  "codex": { "enabled": false },
  "fireworks": { "enabled": true },
  "kimi": { "enabled": true },
  "opencode": { "enabled": true }
}' --json
```

`enabled` defaults to `true` for every discovered agent; set it to `false` to
hide a subscription that is installed. Disabled agents are also skipped when
the records regenerate.

With `syncMode` on, every `*.json` snapshot in `syncDir` is merged, so today,
the last 7 days, and the all-time totals cover every machine you code on —
active days are unioned by date rather than summed. Rate limits stay
per-account and are never merged. A record may declare `"scope": "account"`
when its stats are account-global rather than machine-local (Fireworks'
billing API); those merge by taking the widest value instead of summing, so
the same account synced from two machines is not counted twice.

One caveat on "all-time": the Codex collector only reads native session files
touched in the last 30 days, and Fireworks requests the last 30 days from its
billing API, so their totals and day counts cover that window. Claude's cover
every transcript still on disk.
