#!/usr/bin/env python3
"""Write an OpenCode Go usage record for the Omarchy agents panel.

The collector reads only local OpenCode data. It sends the stored Go credential
only to OpenCode's documented usage endpoint. It never writes credentials or
their values to a record, cache, or log.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import urllib.error
import urllib.request
from typing import Any


AGENT_ID = "opencode"
AGENT_NAME = "OpenCode"
GO_PROVIDER_ID = "opencode-go"
OPEN_CODE_PROVIDER_IDS = {"opencode", GO_PROVIDER_ID}
CACHE_VERSION = 1
USAGE_URL = "https://opencode.ai/zen/go/v1/usage"
CONSOLE_GO_STATUS_URL = "https://opencode.ai/console/api/go/status"
USER_AGENT = "omarchy-agents/1.0"


def xdg_home(name: str, fallback: Path) -> Path:
    return Path(os.environ.get(name) or fallback)


def data_dir() -> Path:
    return xdg_home("XDG_DATA_HOME", Path.home() / ".local" / "share")


def state_dir() -> Path:
    return xdg_home("XDG_STATE_HOME", Path.home() / ".local" / "state")


def cache_dir() -> Path:
    return xdg_home("XDG_CACHE_HOME", Path.home() / ".cache") / "omarchy-agents"


def database_path() -> Path:
    return data_dir() / "opencode" / "opencode.db"


def auth_path() -> Path:
    return data_dir() / "opencode" / "auth.json"


def cache_path() -> Path:
    return cache_dir() / "opencode-go-v1.json"


def record_path() -> Path:
    return state_dir() / "omarchy" / "agents" / "usage" / "opencode.json"


def number(value: Any) -> int:
    try:
        result = int(value or 0)
    except (TypeError, ValueError, OverflowError):
        return 0
    if not math.isfinite(result):
        return 0
    return max(0, result)


def empty_bucket() -> dict[str, int]:
    return {
        "inputTokens": 0,
        "outputTokens": 0,
        "cacheReadInputTokens": 0,
        "cacheCreationInputTokens": 0,
    }


def empty_aggregate() -> dict[str, Any]:
    return {
        "totalPrompts": 0,
        "sessionIds": [],
        "activeDates": [],
        "modelUsage": {},
        "days": {},
    }


def atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, separators=(",", ":"))
            stream.write("\n")
        os.replace(temporary, path)
    except Exception:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def read_json(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def normalise_aggregate(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return empty_aggregate()
    aggregate = empty_aggregate()
    aggregate["totalPrompts"] = number(value.get("totalPrompts"))
    aggregate["sessionIds"] = sorted({str(item) for item in value.get("sessionIds", []) if item})
    aggregate["activeDates"] = sorted({str(item) for item in value.get("activeDates", []) if item})
    aggregate["modelUsage"] = clean_model_usage(value.get("modelUsage"))
    aggregate["days"] = clean_days(value.get("days"))
    return aggregate


def clean_bucket(value: Any) -> dict[str, int]:
    if not isinstance(value, dict):
        return empty_bucket()
    return {
        "inputTokens": number(value.get("inputTokens")),
        "outputTokens": number(value.get("outputTokens")),
        "cacheReadInputTokens": number(value.get("cacheReadInputTokens")),
        "cacheCreationInputTokens": number(value.get("cacheCreationInputTokens")),
    }


def clean_model_usage(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    cleaned: dict[str, Any] = {}
    for key, bucket in value.items():
        if key and isinstance(bucket, dict):
            cleaned[str(key)] = clean_bucket(bucket)
    return cleaned


def clean_days(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    cleaned: dict[str, Any] = {}
    for key, day in value.items():
        if not key or not isinstance(day, dict):
            continue
        sessions = day.get("sessions")
        models = day.get("models")
        cleaned[str(key)] = {
            "tokens": number(day.get("tokens")),
            "prompts": number(day.get("prompts")),
            "sessions": sorted({str(item) for item in sessions if item}) if isinstance(sessions, list) else [],
            "models": {str(name): number(total) for name, total in models.items() if name} if isinstance(models, dict) else {},
        }
    return cleaned


def read_cache(path: Path) -> dict[str, Any]:
    cached = read_json(path)
    if not cached or cached.get("version") != CACHE_VERSION:
        return {"version": CACHE_VERSION, "aggregate": empty_aggregate(), "lastRowId": 0, "database": {}, "limits": []}
    cached["aggregate"] = normalise_aggregate(cached.get("aggregate"))
    cached["lastRowId"] = number(cached.get("lastRowId"))
    cached["database"] = cached.get("database") if isinstance(cached.get("database"), dict) else {}
    cached["limits"] = cached.get("limits") if isinstance(cached.get("limits"), list) else []
    return cached


def local_day(milliseconds: Any) -> str:
    timestamp = number(milliseconds)
    if timestamp <= 0:
        return dt.datetime.now().astimezone().date().isoformat()
    try:
        return dt.datetime.fromtimestamp(timestamp / 1000).astimezone().date().isoformat()
    except (OverflowError, OSError, ValueError):
        return dt.datetime.now().astimezone().date().isoformat()


def add_message(aggregate: dict[str, Any], session_id: str, entry: dict[str, Any]) -> None:
    tokens = entry.get("tokens") if isinstance(entry.get("tokens"), dict) else {}
    cache = tokens.get("cache") if isinstance(tokens.get("cache"), dict) else {}
    input_tokens = number(tokens.get("input"))
    output_tokens = number(tokens.get("output")) + number(tokens.get("reasoning"))
    cache_read = number(cache.get("read"))
    cache_write = number(cache.get("write"))
    total = input_tokens + output_tokens + cache_read + cache_write
    if total <= 0:
        return

    day = local_day((entry.get("time") or {}).get("created"))
    model = str(entry.get("modelID") or "opencode").rstrip("/").split("/")[-1]
    aggregate["totalPrompts"] = number(aggregate.get("totalPrompts")) + 1
    aggregate["sessionIds"] = sorted(set(aggregate.get("sessionIds", [])) | {str(session_id)})
    aggregate["activeDates"] = sorted(set(aggregate.get("activeDates", [])) | {day})

    bucket = aggregate["modelUsage"].setdefault(model, empty_bucket())
    bucket["inputTokens"] = number(bucket.get("inputTokens")) + input_tokens
    bucket["outputTokens"] = number(bucket.get("outputTokens")) + output_tokens
    bucket["cacheReadInputTokens"] = number(bucket.get("cacheReadInputTokens")) + cache_read
    bucket["cacheCreationInputTokens"] = number(bucket.get("cacheCreationInputTokens")) + cache_write

    daily = aggregate["days"].setdefault(day, {"tokens": 0, "prompts": 0, "sessions": [], "models": {}})
    daily["tokens"] = number(daily.get("tokens")) + total
    daily["prompts"] = number(daily.get("prompts")) + 1
    daily["sessions"] = sorted(set(daily.get("sessions", [])) | {str(session_id)})
    daily_models = daily.setdefault("models", {})
    daily_models[model] = number(daily_models.get(model)) + total


def database_identity(path: Path) -> dict[str, int]:
    stat = path.stat()
    return {"device": stat.st_dev, "inode": stat.st_ino, "size": stat.st_size, "mtime": stat.st_mtime_ns}


def database_reusable(previous: Any, identity: dict[str, int]) -> bool:
    """Same file (device+inode) that did not shrink stays incremental and
    rowid-based: normal appends often fit in already-allocated pages without
    growing the file, so size/mtime alone cannot judge. A replaced database
    is caught downstream by max(rowid) < lastRowId, which restarts from zero.
    """
    if not isinstance(previous, dict):
        return False
    if previous.get("device") != identity["device"] or previous.get("inode") != identity["inode"]:
        return False
    return number(previous.get("size")) <= identity["size"]


def scan_database(path: Path, cached: dict[str, Any], force: bool) -> tuple[dict[str, Any], int, dict[str, int]]:
    identity = database_identity(path)
    previous = cached.get("database") or {}
    reuse = not force and database_reusable(previous, identity)
    aggregate = normalise_aggregate(cached.get("aggregate")) if reuse else empty_aggregate()
    start_row_id = number(cached.get("lastRowId")) if reuse else 0

    connection = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=2)
    try:
        connection.execute("PRAGMA query_only = ON")
        max_row_id = number(connection.execute("SELECT COALESCE(MAX(rowid), 0) FROM message").fetchone()[0])
        if max_row_id < start_row_id:
            aggregate = empty_aggregate()
            start_row_id = 0
        rows = connection.execute("SELECT rowid, session_id, data FROM message WHERE rowid > ? ORDER BY rowid", (start_row_id,))
        last_row_id = start_row_id
        for row_id, session_id, raw in rows:
            last_row_id = number(row_id)
            try:
                entry = json.loads(raw)
            except (TypeError, json.JSONDecodeError):
                continue
            if not isinstance(entry, dict) or entry.get("role") != "assistant":
                continue
            if str(entry.get("providerID") or "") not in OPEN_CODE_PROVIDER_IDS:
                continue
            add_message(aggregate, str(session_id), entry)
    finally:
        connection.close()
    return aggregate, last_row_id, identity


def stats_from_aggregate(aggregate: dict[str, Any]) -> dict[str, Any]:
    today = dt.datetime.now().astimezone().date()
    dates = [(today - dt.timedelta(days=offset)).isoformat() for offset in range(6, -1, -1)]
    days = aggregate.get("days") or {}
    current = days.get(today.isoformat()) or {}
    return {
        "todayPrompts": number(current.get("prompts")),
        "todaySessions": len(set(current.get("sessions", []))),
        "todayTotalTokens": number(current.get("tokens")),
        "todayTokensByModel": current.get("models") if isinstance(current.get("models"), dict) else {},
        "recentDays": [{"date": day, "messageCount": number((days.get(day) or {}).get("tokens"))} for day in dates],
        "modelUsage": aggregate.get("modelUsage") or {},
        "totalPrompts": number(aggregate.get("totalPrompts")),
        "totalSessions": len(set(aggregate.get("sessionIds", []))),
        "activeDays": len(set(aggregate.get("activeDates", []))),
        "activeDates": sorted(set(aggregate.get("activeDates", []))),
    }


def go_credential(path: Path) -> str:
    data = read_json(path) or {}
    candidate = data.get(GO_PROVIDER_ID)
    if isinstance(candidate, str):
        return candidate.strip()
    if not isinstance(candidate, dict):
        return ""
    for key in ("key", "apiKey", "token", "accessToken"):
        value = candidate.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def oauth_session(path: Path) -> tuple[str, str]:
    """Return the active OpenCode Console access token and organization ID.

    OpenCode's browser device flow stores this account in its SQLite database,
    not in auth.json. The values stay in memory and never enter a record.
    """
    if not path.is_file():
        return "", ""
    try:
        connection = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=2)
        try:
            row = connection.execute(
                """
                SELECT account.access_token, account_state.active_org_id
                FROM account_state
                JOIN account ON account.id = account_state.active_account_id
                LIMIT 1
                """
            ).fetchone()
        finally:
            connection.close()
    except (OSError, sqlite3.Error):
        return "", ""
    if not row:
        return "", ""
    token = row[0] if isinstance(row[0], str) else ""
    organization = row[1] if isinstance(row[1], str) else ""
    return token.strip(), organization.strip()


def limits_from_payload(payload: dict[str, Any]) -> list[dict[str, Any]]:
    usage = payload.get("usage") if isinstance(payload.get("usage"), dict) else {}
    labels = (("rolling", "Session (5-hour)"), ("weekly", "Weekly (7-day)"), ("monthly", "Monthly"))
    limits = []
    for key, label in labels:
        window = usage.get(key)
        if not isinstance(window, dict):
            continue
        try:
            percent = float(window.get("percent")) / 100.0
        except (TypeError, ValueError):
            continue
        if not math.isfinite(percent) or percent < 0:
            continue
        limits.append({"label": label, "percent": min(1.0, percent), "resetsAt": str(window.get("resetsAt") or "")})
    return limits


def limits_from_console_payload(payload: dict[str, Any]) -> list[dict[str, Any]]:
    access = payload.get("access") if isinstance(payload.get("access"), dict) else {}
    meters = access.get("meters") if isinstance(access.get("meters"), dict) else {}
    windows = (
        ("fiveHour", "Session (5-hour)"),
        ("week", "Weekly (7-day)"),
        ("month", "Monthly"),
    )
    limits = []
    for key, label in windows:
        meter = meters.get(key)
        if not isinstance(meter, dict):
            continue
        try:
            used = int(meter.get("usedMicroCents", 0))
            limit = int(meter.get("limitMicroCents", 0))
        except (TypeError, ValueError, OverflowError):
            continue
        if used < 0 or limit <= 0:
            continue
        limits.append({
            "label": label,
            "percent": min(1.0, used / limit),
            "resetsAt": str(meter.get("resetsAt") or ""),
        })
    return limits


def fetch_limits(token: str, url: str = USAGE_URL) -> tuple[list[dict[str, Any]] | None, str, bool]:
    request = urllib.request.Request(
        url,
        headers={
            "Authorization": "Bearer " + token,
            "Accept": "application/json",
            "User-Agent": USER_AGENT,
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            payload = json.loads(response.read().decode("utf-8"))
        if not isinstance(payload, dict):
            return None, "OpenCode Go limits unavailable.", True
        return limits_from_payload(payload), "", False
    except urllib.error.HTTPError as error:
        if error.code in (401, 403):
            return None, "OpenCode Go authentication expired. Run /connect in OpenCode.", False
        return None, "OpenCode Go limits unavailable.", True
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError, ValueError):
        return None, "OpenCode Go limits unavailable.", True


def fetch_console_limits(token: str, organization: str, url: str = CONSOLE_GO_STATUS_URL) -> tuple[list[dict[str, Any]] | None, str, bool]:
    request = urllib.request.Request(
        url,
        headers={
            "Authorization": "Bearer " + token,
            "Accept": "application/json",
            "x-org-id": organization,
            "User-Agent": USER_AGENT,
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            payload = json.loads(response.read().decode("utf-8"))
        if not isinstance(payload, dict):
            return None, "OpenCode Go limits unavailable.", True
        return limits_from_console_payload(payload), "", False
    except urllib.error.HTTPError as error:
        if error.code in (401, 403):
            return None, "OpenCode account session expired. Log in again with the OpenCode account flow.", False
        return None, "OpenCode Go limits unavailable.", True
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError, ValueError):
        return None, "OpenCode Go limits unavailable.", True


def base_record(stats: dict[str, Any], limits: list[dict[str, Any]], tier_label: str, status: str = "", help_text: str = "", retry: bool = False) -> dict[str, Any]:
    record = {
        "schemaVersion": 1,
        "id": AGENT_ID,
        "name": AGENT_NAME,
        "updatedAt": dt.datetime.now(dt.timezone.utc).isoformat(),
        "ready": number(stats.get("totalPrompts")) > 0 or bool(limits),
        "hasLocalStats": True,
        "tierLabel": tier_label,
        "usageStatusText": status,
        "authHelpText": help_text,
        "limits": limits,
    }
    if retry:
        record["retryAdvised"] = True
    record.update(stats)
    return record


def apply_fresh_limits(fresh_limits: list[dict[str, Any]] | None, cached_limits: list[dict[str, Any]],
                        status: str, retry: bool, stats: dict[str, Any]) -> tuple[list[dict[str, Any]], str, bool]:
    """Merge a limits fetch into cached windows.

    A reachable endpoint that reports no windows must not wipe the cache
    with zeros presented as fresh quota: keep the cached windows and retry
    soon instead.
    """
    if fresh_limits:
        return fresh_limits, "", False
    if fresh_limits is not None:
        # Reachable but windowless.
        if cached_limits or number(stats.get("totalPrompts")) > 0:
            return cached_limits, "", True
        return [], status, True
    if cached_limits:
        return cached_limits, "", retry
    return [], status, retry


def collect(force: bool, limits_only: bool) -> dict[str, Any]:
    db = database_path()
    cache_file = cache_path()
    cache = read_cache(cache_file)
    stats = stats_from_aggregate(cache["aggregate"])
    if db.is_file() and not limits_only:
        try:
            aggregate, last_row_id, identity = scan_database(db, cache, force)
            cache["aggregate"] = aggregate
            cache["lastRowId"] = last_row_id
            cache["database"] = identity
            stats = stats_from_aggregate(aggregate)
        except (OSError, sqlite3.Error):
            pass

    oauth_token, organization = oauth_session(db)
    token = go_credential(auth_path())
    status = ""
    help_text = ""
    retry = False
    limits = cache.get("limits") or []
    tier_label = "OpenCode"
    if oauth_token and organization:
        tier_label = "Go"
        fresh_limits, status, retry = fetch_console_limits(oauth_token, organization)
        limits, help_text, retry = apply_fresh_limits(
            fresh_limits, limits, status, retry, stats)
        if fresh_limits:
            cache["limits"] = limits
    elif not token:
        limits = []
    else:
        tier_label = "Go"
        fresh_limits, status, retry = fetch_limits(token)
        limits, help_text, retry = apply_fresh_limits(
            fresh_limits, limits, status, retry, stats)
        if fresh_limits:
            cache["limits"] = limits
    try:
        atomic_json(cache_file, cache)
    except OSError:
        pass
    return base_record(stats, limits, tier_label, status, help_text, retry)


def purge(confirmed: bool) -> int:
    if not confirmed:
        print("Refusing to remove usage data without --yes.", file=sys.stderr)
        return 2
    for path in (cache_path(), record_path()):
        try:
            path.unlink()
        except FileNotFoundError:
            pass
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Write an OpenCode Go usage record for Omarchy.")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--limits-only", action="store_true")
    parser.add_argument("--purge", action="store_true")
    parser.add_argument("--yes", action="store_true")
    args = parser.parse_args()
    if args.purge:
        return purge(args.yes)
    record = collect(args.force, args.limits_only)
    atomic_json(record_path(), record)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
