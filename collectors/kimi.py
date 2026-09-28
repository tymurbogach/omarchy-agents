#!/usr/bin/env python3
"""Write a Kimi Code usage record for the Omarchy agents panel.

The collector reads only local Kimi Code data: native session wire logs and
the OpenCode database rows that ran on a Kimi provider. It refreshes the
stored OAuth credential in memory only, sends it solely to Kimi's own
endpoints, and never writes credentials or their values to a record, cache,
or log.
"""

from __future__ import annotations

import argparse
import configparser
import datetime as dt
import glob
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any


AGENT_ID = "kimi"
AGENT_NAME = "Kimi Code"
# OpenCode provider ids that burn Kimi quota through third-party tools.
OPENCODE_KIMI_PROVIDER_IDS = {"kimi-for-coding", "moonshot", "kimi"}
CACHE_VERSION = 1
OAUTH_CLIENT_ID = "17e5f671-d194-4dfb-9706-5516cb48c098"
OAUTH_HOSTS = ("https://auth.kimi.ai", "https://auth.kimi.com")
API_BASES = ("https://api.kimi.ai/coding/v1", "https://api.kimi.com/coding/v1")
USER_AGENT = "omarchy-agents/1.0"


def xdg_home(name: str, fallback: Path) -> Path:
    return Path(os.environ.get(name) or fallback)


def home() -> Path:
    return Path.home()


def kimi_dir() -> Path:
    return home() / ".kimi-code"


def sessions_dir() -> Path:
    return kimi_dir() / "sessions"


def config_path() -> Path:
    return kimi_dir() / "config.toml"


def cache_path() -> Path:
    return xdg_home("XDG_CACHE_HOME", home() / ".cache") / "omarchy-agents" / "kimi-v1.json"


def record_path() -> Path:
    return xdg_home("XDG_STATE_HOME", home() / ".local" / "state") / "omarchy" / "agents" / "usage" / "kimi.json"


def opencode_database_path() -> Path:
    return xdg_home("XDG_DATA_HOME", home() / ".local" / "share") / "opencode" / "opencode.db"


def number(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


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
        "promptIds": [],
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
    aggregate["promptIds"] = sorted({str(item) for item in value.get("promptIds", []) if item})
    aggregate["sessionIds"] = sorted({str(item) for item in value.get("sessionIds", []) if item})
    aggregate["activeDates"] = sorted({str(item) for item in value.get("activeDates", []) if item})
    aggregate["modelUsage"] = value.get("modelUsage") if isinstance(value.get("modelUsage"), dict) else {}
    aggregate["days"] = value.get("days") if isinstance(value.get("days"), dict) else {}
    return aggregate


def read_cache(path: Path) -> dict[str, Any]:
    cached = read_json(path)
    if not cached or cached.get("version") != CACHE_VERSION:
        return {"version": CACHE_VERSION, "aggregate": empty_aggregate(), "wires": {}, "database": {}, "limits": []}
    cached["aggregate"] = normalise_aggregate(cached.get("aggregate"))
    cached["wires"] = cached.get("wires") if isinstance(cached.get("wires"), dict) else {}
    cached["database"] = cached.get("database") if isinstance(cached.get("database"), dict) else {}
    cached["limits"] = cached.get("limits") if isinstance(cached.get("limits"), list) else []
    return cached


def local_day_from_ms(milliseconds: Any) -> str:
    timestamp = number(milliseconds)
    if timestamp <= 0:
        return dt.datetime.now().astimezone().date().isoformat()
    return dt.datetime.fromtimestamp(timestamp / 1000).astimezone().date().isoformat()


def short_model(model_id: str) -> str:
    return str(model_id or "kimi").rstrip("/").split("/")[-1] or "kimi"


def add_tokens(aggregate: dict[str, Any], session_key: str, model: str, day: str,
               input_tokens: int, output_tokens: int, cache_read: int, cache_write: int) -> None:
    total = input_tokens + output_tokens + cache_read + cache_write
    if total <= 0:
        return
    aggregate["sessionIds"] = sorted(set(aggregate.get("sessionIds", [])) | {session_key})
    aggregate["activeDates"] = sorted(set(aggregate.get("activeDates", [])) | {day})

    bucket = aggregate["modelUsage"].setdefault(model, empty_bucket())
    bucket["inputTokens"] = number(bucket.get("inputTokens")) + input_tokens
    bucket["outputTokens"] = number(bucket.get("outputTokens")) + output_tokens
    bucket["cacheReadInputTokens"] = number(bucket.get("cacheReadInputTokens")) + cache_read
    bucket["cacheCreationInputTokens"] = number(bucket.get("cacheCreationInputTokens")) + cache_write

    daily = aggregate["days"].setdefault(day, {"tokens": 0, "prompts": 0, "sessions": [], "models": {}})
    daily["tokens"] = number(daily.get("tokens")) + total
    daily["sessions"] = sorted(set(daily.get("sessions", [])) | {session_key})
    daily_models = daily.setdefault("models", {})
    daily_models[model] = number(daily_models.get(model)) + total


def add_prompt(aggregate: dict[str, Any], session_key: str, prompt_id: str, day: str) -> None:
    if not prompt_id or prompt_id in set(aggregate.get("promptIds", [])):
        return
    aggregate["promptIds"] = sorted(set(aggregate.get("promptIds", [])) | {prompt_id})
    aggregate["totalPrompts"] = number(aggregate.get("totalPrompts")) + 1
    aggregate["sessionIds"] = sorted(set(aggregate.get("sessionIds", [])) | {session_key})
    aggregate["activeDates"] = sorted(set(aggregate.get("activeDates", [])) | {day})
    daily = aggregate["days"].setdefault(day, {"tokens": 0, "prompts": 0, "sessions": [], "models": {}})
    daily["prompts"] = number(daily.get("prompts")) + 1
    daily["sessions"] = sorted(set(daily.get("sessions", [])) | {session_key})


def session_id_for_wire(path: Path) -> str:
    # .../sessions/<workspace>/session_<uuid>/agents/<agent>/wire.jsonl
    parts = path.parts
    for index, part in enumerate(parts):
        if part.startswith("session_") and index > 0:
            return "kimi:" + part
    return "kimi:" + path.parent.name


def scan_wire_file(path: Path, aggregate: dict[str, Any], offset: int) -> int:
    session_key = session_id_for_wire(path)
    position = offset
    try:
        with open(path, "r", encoding="utf-8") as stream:
            if offset > 0:
                stream.seek(offset)
            # Byte-count manually: tell() is unreliable on a text stream
            # advanced by next(), and readline keeps offsets exact.
            while True:
                line = stream.readline()
                if line == "":
                    break
                position += len(line.encode("utf-8"))
                line = line.strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(event, dict):
                    continue
                kind = event.get("type")
                moment = event.get("time")
                if kind == "usage.record" and event.get("usageScope") == "turn":
                    usage = event.get("usage") if isinstance(event.get("usage"), dict) else {}
                    add_tokens(
                        aggregate, session_key, short_model(str(event.get("model") or "kimi")),
                        local_day_from_ms(moment),
                        number(usage.get("inputOther")) + number(usage.get("input")),
                        number(usage.get("output")),
                        number(usage.get("inputCacheRead")),
                        number(usage.get("inputCacheCreation")),
                    )
                elif kind == "turn.prompt":
                    add_prompt(aggregate, session_key, str(event.get("promptId") or ""),
                               local_day_from_ms(moment))
    except (OSError, ValueError):
        return offset
    return position


def scan_wires(root: Path, cached: dict[str, Any], force: bool) -> tuple[dict[str, Any], dict[str, Any], bool]:
    known = cached.get("wires") or {}
    current = sorted(glob.glob(str(root / "*" / "session_*" / "agents" / "*" / "wire.jsonl")))
    rebuilt = True
    if force:
        aggregate: dict[str, Any] = empty_aggregate()
        wires: dict[str, Any] = {}
    else:
        aggregate = normalise_aggregate(cached.get("aggregate"))
        wires = dict(known)
        shrunk = False
        for path in current:
            try:
                size = Path(path).stat().st_size
            except OSError:
                continue
            previous = known.get(path)
            if isinstance(previous, dict) and number(previous.get("size")) > size:
                shrunk = True
                break
        if shrunk or set(current) != set(known):
            # A rotated or removed log invalidates offsets: rebuild from scratch.
            aggregate = empty_aggregate()
            wires = {}
        else:
            rebuilt = False
    for path in current:
        try:
            size = Path(path).stat().st_size
        except OSError:
            continue
        entry = wires.get(path)
        offset = 0
        if not rebuilt and isinstance(entry, dict):
            offset = min(number(entry.get("offset")), size)
        offset = scan_wire_file(Path(path), aggregate, offset)
        wires[path] = {"offset": offset, "size": size}
    return aggregate, wires, rebuilt


def database_identity(path: Path) -> dict[str, int]:
    stat = path.stat()
    return {"device": stat.st_dev, "inode": stat.st_ino, "size": stat.st_size}


def scan_opencode_database(path: Path, aggregate: dict[str, Any], cached: dict[str, Any],
                           rebuilt: bool, force: bool) -> tuple[dict[str, Any], int]:
    """Add OpenCode rows that ran on a Kimi provider (exact providerID match)."""
    try:
        identity = database_identity(path)
    except OSError:
        return aggregate, number((cached.get("database") or {}).get("lastRowId"))
    previous = cached.get("database") or {}
    reuse = (not force and not rebuilt and previous.get("device") == identity["device"]
             and previous.get("inode") == identity["inode"]
             and number(previous.get("size")) <= identity["size"])
    start_row_id = number(previous.get("lastRowId")) if reuse else 0
    last_row_id = start_row_id
    try:
        connection = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=2)
    except (OSError, sqlite3.Error):
        return aggregate, last_row_id
    try:
        connection.execute("PRAGMA query_only = ON")
        try:
            max_row_id = number(connection.execute("SELECT COALESCE(MAX(rowid), 0) FROM message").fetchone()[0])
            if max_row_id < start_row_id:
                start_row_id = 0
            rows = connection.execute(
                "SELECT rowid, session_id, data FROM message WHERE rowid > ? ORDER BY rowid", (start_row_id,))
        except sqlite3.Error:
            return aggregate, last_row_id
        for row_id, session_id, raw in rows:
            last_row_id = number(row_id)
            try:
                entry = json.loads(raw)
            except (TypeError, json.JSONDecodeError):
                continue
            if not isinstance(entry, dict) or entry.get("role") != "assistant":
                continue
            if str(entry.get("providerID") or "") not in OPENCODE_KIMI_PROVIDER_IDS:
                continue
            tokens = entry.get("tokens") if isinstance(entry.get("tokens"), dict) else {}
            cache = tokens.get("cache") if isinstance(tokens.get("cache"), dict) else {}
            # Opencode keeps thinking tokens out of output; both are generated.
            add_tokens(
                aggregate, "opencode:" + str(session_id),
                short_model(str(entry.get("modelID") or "kimi")),
                local_day_from_ms((entry.get("time") or {}).get("created")),
                number(tokens.get("input")),
                number(tokens.get("output")) + number(tokens.get("reasoning")),
                number(cache.get("read")),
                number(cache.get("write")),
            )
    finally:
        connection.close()
    return aggregate, last_row_id


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


def configured_endpoints() -> tuple[list[str], list[str]]:
    """Read the managed provider's hosts from config.toml (names only)."""
    api_bases = list(API_BASES)
    oauth_hosts = list(OAUTH_HOSTS)
    try:
        text = config_path().read_text(encoding="utf-8")
    except OSError:
        return api_bases, oauth_hosts
    try:
        parser = configparser.ConfigParser()
        parser.read_string(text)
    except configparser.Error:
        return api_bases, oauth_hosts
    for section in parser.sections():
        if section != 'providers."managed:kimi-code"':
            continue
        base = parser.get(section, "base_url", fallback="").strip().rstrip("/")
        host = parser.get(section, "oauth_host", fallback="").strip().rstrip("/")
        if base and base not in api_bases:
            api_bases.insert(0, base)
        if host and host not in oauth_hosts:
            oauth_hosts.insert(0, host)
    return api_bases, oauth_hosts


def stored_credential() -> dict[str, Any]:
    candidates = sorted(glob.glob(str(kimi_dir() / "credentials" / "*.json")))
    for path in candidates:
        data = read_json(Path(path))
        if isinstance(data, dict) and isinstance(data.get("refresh_token"), str) and data["refresh_token"]:
            return data
    return {}


def refresh_access_token(refresh_token: str, oauth_hosts: list[str]) -> str:
    payload = urllib.parse.urlencode({
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "client_id": OAUTH_CLIENT_ID,
    }).encode()
    for host in oauth_hosts:
        request = urllib.request.Request(
            host + "/api/oauth/token",
            data=payload,
            headers={"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json", "User-Agent": USER_AGENT},
        )
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                data = json.loads(response.read().decode("utf-8"))
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError):
            continue
        except urllib.error.HTTPError:
            continue
        if isinstance(data, dict) and isinstance(data.get("access_token"), str) and data["access_token"]:
            return data["access_token"]
    return ""


def access_token(credential: dict[str, Any], oauth_hosts: list[str]) -> str:
    token = credential.get("access_token") if isinstance(credential.get("access_token"), str) else ""
    if token and number(credential.get("expires_at")) - int(time.time()) > 60:
        return token
    refresh = credential.get("refresh_token") if isinstance(credential.get("refresh_token"), str) else ""
    if not refresh:
        return token
    # In memory only: the CLI owns the credentials file, never overwrite it.
    return refresh_access_token(refresh, oauth_hosts) or token


def fetch_json(url: str, token: str) -> dict[str, Any] | None:
    request = urllib.request.Request(
        url,
        headers={"Authorization": "Bearer " + token, "Accept": "application/json", "User-Agent": USER_AGENT},
    )
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError):
        return None
    except urllib.error.HTTPError as error:
        if error.code in (401, 403):
            return None
        return None
    return payload if isinstance(payload, dict) else None


def limits_from_usage_payload(payload: dict[str, Any]) -> list[dict[str, Any]]:
    usages = payload.get("usages") if isinstance(payload.get("usages"), dict) else {}
    windows = (
        ("limit_5h", "Session (5-hour)"),
        ("limit_7d", "Weekly (7-day)"),
        ("limit_month_total", "Monthly"),
        ("limit_month_code", "Monthly"),
    )
    limits = []
    for key, label in windows:
        entry = usages.get(key)
        if not isinstance(entry, dict):
            continue
        try:
            percent = float(entry.get("used_ratio"))
        except (TypeError, ValueError):
            continue
        if percent < 0:
            continue
        limits.append({"label": label, "percent": min(1.0, percent), "resetsAt": str(entry.get("reset_time") or "")})
    return limits


def collect(force: bool, limits_only: bool) -> dict[str, Any]:
    cache_file = cache_path()
    cache = read_cache(cache_file)
    if limits_only:
        aggregate = normalise_aggregate(cache["aggregate"])
        wires = cache["wires"]
        last_row_id = number((cache.get("database") or {}).get("lastRowId"))
    else:
        aggregate, wires, rebuilt = scan_wires(sessions_dir(), cache, force)
        last_row_id = 0
        database = opencode_database_path()
        if database.is_file():
            aggregate, last_row_id = scan_opencode_database(database, aggregate, cache, rebuilt, force)

    stats = stats_from_aggregate(aggregate)
    api_bases, oauth_hosts = configured_endpoints()
    credential = stored_credential()
    tier_label = "Kimi Code"
    status = ""
    help_text = ""
    retry = False
    limits = cache.get("limits") or []
    token = access_token(credential, oauth_hosts) if credential else ""
    if token:
        fresh_limits: list[dict[str, Any]] | None = None
        reachable = False
        for base in api_bases:
            payload = fetch_json(base + "/usages", token)
            if payload is None:
                continue
            reachable = True
            fresh_limits = limits_from_usage_payload(payload)
            profile = fetch_json(base + "/me", token)
            if isinstance(profile, dict) and str(profile.get("user_level_name") or "").strip():
                tier_label = str(profile["user_level_name"]).strip()
            break
        if fresh_limits:
            limits = fresh_limits
            cache["limits"] = limits
        elif not limits:
            if not reachable:
                status = "Kimi Code limits unavailable."
                retry = True
            else:
                help_text = "Run /login in Kimi Code to restore quota."
    else:
        limits = []
        if number(stats.get("totalPrompts")) <= 0:
            help_text = "Run /login in Kimi Code to authenticate."
    try:
        database = opencode_database_path()
        try:
            identity: dict[str, Any] = dict(database_identity(database))
        except OSError:
            identity = {}
        identity["lastRowId"] = last_row_id
        atomic_json(cache_file, {"version": CACHE_VERSION, "aggregate": aggregate, "wires": wires,
                                 "database": identity, "limits": limits})
    except OSError:
        pass
    return base_record(stats, limits, tier_label, status, help_text, retry)


def base_record(stats: dict[str, Any], limits: list[dict[str, Any]], tier_label: str,
                status: str = "", help_text: str = "", retry: bool = False) -> dict[str, Any]:
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
    parser = argparse.ArgumentParser(description="Write a Kimi Code usage record for Omarchy.")
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
