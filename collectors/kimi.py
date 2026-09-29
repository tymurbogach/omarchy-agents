#!/usr/bin/env python3
"""Write a Kimi Code usage record for the Omarchy agents panel.

The collector reads only local Kimi Code data: native session wire logs and
the OpenCode database rows that ran on a Kimi provider. It sends the stored
OAuth credential solely to Kimi's own endpoints, persists a rotated
credential back to the CLI's credentials file (atomic write, mode 0600,
compare-and-swap against concurrent CLI refreshes), and never writes
credentials or their values to a record, cache, or log.
"""

from __future__ import annotations

import argparse
import datetime as dt
import glob
import json
import math
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import time
import tomllib
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
    # The CLI relocates its whole data root with KIMI_CODE_HOME; sessions,
    # config, and credentials all move together.
    override = os.environ.get("KIMI_CODE_HOME", "").strip()
    if override:
        return Path(override).expanduser()
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
        return {"version": CACHE_VERSION, "aggregate": empty_aggregate(), "wires": {}, "database": {}, "limits": [], "tier": ""}
    cached["aggregate"] = normalise_aggregate(cached.get("aggregate"))
    cached["wires"] = cached.get("wires") if isinstance(cached.get("wires"), dict) else {}
    cached["database"] = cached.get("database") if isinstance(cached.get("database"), dict) else {}
    cached["limits"] = cached.get("limits") if isinstance(cached.get("limits"), list) else []
    cached["tier"] = cached.get("tier") if isinstance(cached.get("tier"), str) else ""
    return cached


def local_day_from_ms(milliseconds: Any) -> str:
    timestamp = number(milliseconds)
    if timestamp <= 0:
        return dt.datetime.now().astimezone().date().isoformat()
    try:
        return dt.datetime.fromtimestamp(timestamp / 1000).astimezone().date().isoformat()
    except (OverflowError, OSError, ValueError):
        return dt.datetime.now().astimezone().date().isoformat()


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


def wire_identity(path: Path) -> dict[str, int] | None:
    try:
        stat = path.stat()
    except OSError:
        return None
    return {"device": stat.st_dev, "inode": stat.st_ino, "size": stat.st_size, "mtime": stat.st_mtime_ns}


def scan_wire_file(path: Path, aggregate: dict[str, Any], offset: int) -> int:
    session_key = session_id_for_wire(path)
    position = offset
    try:
        with open(path, "rb") as stream:
            if offset > 0:
                stream.seek(offset)
            while True:
                raw = stream.readline()
                if raw == b"":
                    break
                position += len(raw)
                try:
                    line = raw.decode("utf-8").strip()
                except UnicodeDecodeError:
                    continue
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
        replaced = False
        for path in current:
            identity = wire_identity(Path(path))
            if identity is None:
                continue
            previous = known.get(path)
            if not isinstance(previous, dict):
                continue
            # Same inode and growing size is an append: keep the offset.
            # Anything else (new inode, shrink, same-size rewrite) means the
            # log was rotated or replaced and offsets are garbage.
            if number(previous.get("inode")) != identity["inode"]:
                replaced = True
                break
            if identity["size"] < number(previous.get("size")):
                replaced = True
                break
            if (identity["size"] == number(previous.get("size"))
                    and number(previous.get("mtime")) != identity["mtime"]):
                replaced = True
                break
        if replaced or set(current) != set(known):
            # A rotated or removed log invalidates offsets: rebuild from scratch.
            aggregate = empty_aggregate()
            wires = {}
        else:
            rebuilt = False
    for path in current:
        identity = wire_identity(Path(path))
        if identity is None:
            continue
        entry = wires.get(path)
        offset = 0
        if not rebuilt and isinstance(entry, dict):
            offset = min(number(entry.get("offset")), identity["size"])
        offset = scan_wire_file(Path(path), aggregate, offset)
        wires[path] = {"offset": offset, "size": identity["size"],
                       "inode": identity["inode"], "mtime": identity["mtime"]}
    return aggregate, wires, rebuilt


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


def scan_opencode_database(path: Path, aggregate: dict[str, Any], cached: dict[str, Any],
                           rebuilt: bool, force: bool) -> tuple[dict[str, Any], int]:
    """Add OpenCode rows that ran on a Kimi provider (exact providerID match)."""
    try:
        identity = database_identity(path)
    except OSError:
        return aggregate, number((cached.get("database") or {}).get("lastRowId"))
    previous = cached.get("database") or {}
    reuse = not force and not rebuilt and database_reusable(previous, identity)
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
        try:
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
        except sqlite3.Error:
            # BUSY/LOCKED mid-scan (checkpoint, concurrent vacuum): keep the
            # rows consumed so far; the next run resumes after last_row_id.
            pass
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


def managed_provider() -> dict[str, str]:
    """Read the managed kimi-code provider block from config.toml.

    Returns base_url, oauth_host, and the oauth credential key name. The
    key is a file reference, not a secret.
    """
    provider = {"base_url": "", "oauth_host": "", "key": "", "storage": "file"}
    try:
        with open(config_path(), "rb") as stream:
            config = tomllib.load(stream)
    except (OSError, tomllib.TOMLDecodeError):
        return provider
    try:
        managed = config.get("providers", {}).get("managed:kimi-code", {})
    except AttributeError:
        return provider
    if not isinstance(managed, dict):
        return provider
    base = managed.get("base_url")
    if isinstance(base, str) and base.strip():
        provider["base_url"] = base.strip().rstrip("/")
    oauth = managed.get("oauth")
    if isinstance(oauth, dict):
        for field in ("oauth_host", "key", "storage"):
            value = oauth.get(field)
            if isinstance(value, str) and value.strip():
                provider[field] = value.strip()
    return provider


def is_https_url(value: str) -> bool:
    try:
        parsed = urllib.parse.urlparse(value)
    except (TypeError, ValueError):
        return False
    return parsed.scheme == "https" and bool(parsed.netloc)


def configured_endpoints() -> tuple[list[str], list[str]]:
    """Managed provider hosts first, compiled-in defaults after.

    A managed host that is not https is ignored: sending a Bearer token
    over cleartext HTTP must never happen because of a config typo.
    """
    provider = managed_provider()
    api_bases = list(API_BASES)
    oauth_hosts = list(OAUTH_HOSTS)
    if provider["base_url"] and provider["base_url"] not in api_bases:
        if is_https_url(provider["base_url"]):
            api_bases.insert(0, provider["base_url"])
        else:
            print("kimi collector: ignoring non-https base_url from config.toml", file=sys.stderr)
    if provider["oauth_host"] and provider["oauth_host"] not in oauth_hosts:
        if is_https_url(provider["oauth_host"]):
            oauth_hosts.insert(0, provider["oauth_host"])
        else:
            print("kimi collector: ignoring non-https oauth_host from config.toml", file=sys.stderr)
    return api_bases, oauth_hosts


def credential_path_for_key(key: str) -> Path | None:
    # The oauth key looks like "oauth/<name>"; the CLI stores it as
    # credentials/<name>.json. Reject traversal: basename only.
    parts = key.replace("\\", "/").split("/")
    if any(part in (".", "..") for part in parts):
        return None
    name = parts[-1].strip() if parts else ""
    if not name or name.startswith("."):
        return None
    if any(sep in name for sep in ("/", "\\")):
        return None
    return kimi_dir() / "credentials" / (name + ".json")


def has_refresh_token(data: Any) -> bool:
    return isinstance(data, dict) and isinstance(data.get("refresh_token"), str) and bool(data["refresh_token"].strip())


def stored_credential() -> tuple[Path | None, dict[str, Any]]:
    """Return the managed OAuth credential file and its content.

    Prefers the credential referenced by config.toml's oauth key; falls
    back to the first credentials file carrying a refresh token.
    """
    provider = managed_provider()
    if provider["key"] and provider.get("storage", "file") == "file":
        path = credential_path_for_key(provider["key"])
        if path is not None:
            data = read_json(path)
            if has_refresh_token(data):
                return path, data
    candidates = sorted(glob.glob(str(kimi_dir() / "credentials" / "*.json")))
    for candidate in candidates:
        path = Path(candidate)
        data = read_json(path)
        if has_refresh_token(data):
            return path, data
    return None, {}


def refresh_threshold_seconds(expires_in: Any) -> int:
    # Same rule as the CLI's OAuthManager: half the lifetime, at least 5 min.
    try:
        lifetime = float(expires_in)
    except (TypeError, ValueError):
        return 300
    if not math.isfinite(lifetime) or lifetime <= 0:
        return 300
    return max(300, int(lifetime * 0.5))


def credential_needs_refresh(credential: dict[str, Any]) -> bool:
    token = credential.get("access_token")
    if not isinstance(token, str) or not token:
        return True
    try:
        remaining = float(credential.get("expires_at", 0)) - time.time()
    except (TypeError, ValueError):
        return True
    if not math.isfinite(remaining):
        return True
    return remaining < refresh_threshold_seconds(credential.get("expires_in"))


def post_refresh_token(refresh_token: str, oauth_hosts: list[str]) -> tuple[dict[str, Any] | None, str | None]:
    """Exchange a refresh token. Returns (token_info, error).

    token_info uses the CLI wire names (snake_case). error is None on
    success, "auth" when the server rejected the grant, "network"
    otherwise. Every host is tried: a 401 from the wrong host must not
    hide the account living on the other one. Never raises, never logs
    secrets.
    """
    payload = urllib.parse.urlencode({
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "client_id": OAUTH_CLIENT_ID,
    }).encode()
    token_info: dict[str, Any] | None = None
    error: str | None = "network"
    auth_rejections = 0
    network_failures = 0
    for host in oauth_hosts:
        request = urllib.request.Request(
            host + "/api/oauth/token",
            data=payload,
            headers={"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json", "User-Agent": USER_AGENT},
        )
        try:
            with urllib.request.urlopen(request, timeout=8) as response:
                data = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as http_error:
            if http_error.code in (401, 403) or is_invalid_grant(http_error):
                auth_rejections += 1
                error = "auth"
                continue
            network_failures += 1
            error = "network"
            continue
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError, ValueError):
            network_failures += 1
            error = "network"
            continue
        if not isinstance(data, dict):
            network_failures += 1
            error = "network"
            continue
        access = data.get("access_token")
        refresh = data.get("refresh_token")
        try:
            lifetime = float(data.get("expires_in"))
        except (TypeError, ValueError):
            lifetime = float("nan")
        if not isinstance(access, str) or not access or not isinstance(refresh, str) or not refresh:
            network_failures += 1
            error = "network"
            continue
        if not math.isfinite(lifetime) or lifetime <= 0:
            network_failures += 1
            error = "network"
            continue
        now = int(time.time())
        token_info = {
            "access_token": access,
            "refresh_token": refresh,
            "expires_at": now + int(lifetime),
            "expires_in": int(lifetime),
            "scope": data.get("scope") if isinstance(data.get("scope"), str) else "",
            "token_type": data.get("token_type") if isinstance(data.get("token_type"), str) else "Bearer",
        }
        error = None
        break
    if token_info is None and oauth_hosts:
        # Mixed outcomes (one host rejects, another is unreachable) must not
        # report auth: the account may live on the unreachable host.
        if auth_rejections >= len(oauth_hosts):
            error = "auth"
        elif network_failures > 0:
            error = "network"
    return token_info, error


def is_invalid_grant(http_error: urllib.error.HTTPError) -> bool:
    """OAuth2 revocations arrive as 400 invalid_grant, not 401."""
    if http_error.code != 400:
        return False
    try:
        body = http_error.read().decode("utf-8", errors="replace")
    except (OSError, ValueError):
        return False
    return "invalid_grant" in body


def save_credential(path: Path, token_info: dict[str, Any]) -> bool:
    """Atomically persist a refreshed credential (mode 0600, fsync).

    Only ever writes the CLI's own wire shape; never touches record/cache.
    Unknown fields already in the file are preserved so a newer CLI that
    stores extras (id_token, issued_at, ...) survives our rotation.
    """
    known = {
        "access_token": token_info.get("access_token", ""),
        "refresh_token": token_info.get("refresh_token", ""),
        "expires_at": number(token_info.get("expires_at")),
        "scope": str(token_info.get("scope") or ""),
        "token_type": str(token_info.get("token_type") or "Bearer"),
        "expires_in": number(token_info.get("expires_in")),
    }
    payload: dict[str, Any] = {}
    existing = read_json(path)
    if isinstance(existing, dict):
        for key, value in existing.items():
            if key not in known:
                payload[key] = value
    payload.update(known)
    try:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        try:
            os.chmod(path.parent, 0o700)
        except OSError:
            pass
        handle, temporary = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".", suffix=".tmp")
        try:
            with os.fdopen(handle, "w", encoding="utf-8") as stream:
                json.dump(payload, stream, indent=2)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
        except Exception:
            try:
                os.unlink(temporary)
            except OSError:
                pass
            return False
        try:
            os.chmod(temporary, 0o600)
            os.replace(temporary, path)
        except OSError:
            try:
                os.unlink(temporary)
            except OSError:
                pass
            return False
        try:
            dir_fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        except OSError:
            pass
        return True
    except OSError:
        return False


def resolve_access_token() -> tuple[str, str | None]:
    """Return (access_token, error) using the CLI-compatible lifecycle.

    Reads the managed credential, refreshes it when within the CLI's
    threshold, and persists the rotation with a compare-and-swap re-read:
    if another CLI process rotated the file while our refresh was in
    flight, the peer's newer credential wins and ours is discarded.
    error is None on success, "auth" when the grant was rejected,
    "network" when the refresh or storage failed.
    """
    oauth_hosts = configured_endpoints()[1]
    path, credential = stored_credential()
    if path is None:
        return "", "auth"
    if not credential_needs_refresh(credential):
        token = credential.get("access_token")
        return (token if isinstance(token, str) else ""), None
    refresh = credential.get("refresh_token")
    if not isinstance(refresh, str) or not refresh:
        return (credential.get("access_token") if isinstance(credential.get("access_token"), str) else ""), "auth"
    used_refresh = refresh
    token_info, error = post_refresh_token(refresh, oauth_hosts)
    if token_info is None:
        # Auth rejection: leave the CLI's file alone (no tombstone from
        # here); the CLI records that itself on its next run.
        if error == "auth":
            return "", "auth"
        # Network failure: report no token so callers keep cached windows and
        # retry soon, instead of probing the API with an expiring token and
        # misreporting the resulting 401 as an expired login.
        return "", "network"
    # Compare-and-swap: re-read before writing; a peer rotation wins.
    current = read_json(path)
    if has_refresh_token(current) and current.get("refresh_token") != used_refresh:
        token = current.get("access_token")
        return (token if isinstance(token, str) else ""), None
    if not save_credential(path, token_info):
        return token_info["access_token"], "network"
    return token_info["access_token"], None


def fetch_json(url: str, token: str) -> tuple[dict[str, Any] | None, str | None]:
    """GET a JSON endpoint. Returns (payload, error).

    error is None on success, "auth" on 401/403, "network" otherwise.
    HTTPError is caught before URLError: it subclasses it.
    """
    request = urllib.request.Request(
        url,
        headers={"Authorization": "Bearer " + token, "Accept": "application/json", "User-Agent": USER_AGENT},
    )
    try:
        with urllib.request.urlopen(request, timeout=8) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as http_error:
        if http_error.code in (401, 403):
            return None, "auth"
        return None, "network"
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError, ValueError):
        return None, "network"
    return (payload if isinstance(payload, dict) else None), (None if isinstance(payload, dict) else "network")


def ratio_used(limit_value: Any, used_value: Any) -> float | None:
    try:
        limit = float(limit_value)
        used = float(used_value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(limit) or not math.isfinite(used):
        return None
    if not limit > 0 or used < 0:
        return None
    return max(0.0, min(1.0, used / limit))


def finite_ratio(value: Any) -> float | None:
    try:
        ratio = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(ratio) or ratio < 0:
        return None
    return max(0.0, min(1.0, ratio))


def window_label_for_duration(duration: Any, unit: Any) -> str:
    """Map a rolling-window duration to a panel label, exactly.

    Only the durations the API actually emits are recognised: 300 minutes
    (5-hour session), 10080 minutes (7-day week), and month-scale windows.
    Anything else is skipped rather than mislabelled.
    """
    try:
        amount = float(duration)
    except (TypeError, ValueError):
        return ""
    if not math.isfinite(amount) or amount <= 0:
        return ""
    text = str(unit or "").upper()
    if "SECOND" in text:
        amount = amount / 60.0
    elif "HOUR" in text:
        amount = amount * 60.0
    elif "DAY" in text:
        amount = amount * 24.0 * 60.0
    elif "MINUTE" not in text:
        return ""
    minutes = int(round(amount))
    if minutes == 300:
        return "Session (5-hour)"
    if minutes == 10080:
        return "Weekly (7-day)"
    if minutes in (43200, 43800, 525600 // 12):
        return "Monthly"
    return ""


def limits_from_usage_payload(payload: dict[str, Any]) -> list[dict[str, Any]]:
    # The `usages.*.used_ratio` block feeds the CLI's /usage display but stays
    # at zero while the server enforces the quota, so it only fills windows
    # the authoritative blocks did not provide. The enforced state lives in
    # top-level `usage` (weekly pool, used/limit) and in `limits[]` (rolling
    # windows, used or remaining over limit).
    limits: list[dict[str, Any]] = []
    seen: set[str] = set()

    def add(label: str, percent: float | None, resets_at: Any) -> None:
        if not label or label in seen or percent is None:
            return
        if not math.isfinite(percent) or percent < 0:
            return
        seen.add(label)
        reset = resets_at if isinstance(resets_at, str) else ""
        limits.append({"label": label, "percent": max(0.0, min(1.0, percent)), "resetsAt": reset})

    head = payload.get("usage") if isinstance(payload.get("usage"), dict) else {}
    add("Weekly (7-day)", ratio_used(head.get("limit"), head.get("used")), head.get("resetTime"))

    windows = payload.get("limits") if isinstance(payload.get("limits"), list) else []
    for window in windows:
        if not isinstance(window, dict):
            continue
        frame = window.get("window") if isinstance(window.get("window"), dict) else {}
        label = window_label_for_duration(frame.get("duration"), frame.get("timeUnit"))
        if not label:
            continue
        detail = window.get("detail") if isinstance(window.get("detail"), dict) else {}
        percent = ratio_used(detail.get("limit"), detail.get("used"))
        if percent is None and detail.get("remaining") is not None:
            remaining = ratio_used(detail.get("limit"), detail.get("remaining"))
            percent = 1.0 - remaining if remaining is not None else None
        add(label, percent, detail.get("resetTime"))

    if not limits:
        usages = payload.get("usages") if isinstance(payload.get("usages"), dict) else {}
        for key, label in (("limit_5h", "Session (5-hour)"), ("limit_7d", "Weekly (7-day)"),
                           ("limit_month_total", "Monthly"), ("limit_month_code", "Monthly")):
            entry = usages.get(key)
            if not isinstance(entry, dict):
                continue
            add(label, finite_ratio(entry.get("used_ratio")), entry.get("reset_time"))
    else:
        # Authoritative windows exist: only fill gaps from the display block.
        usages = payload.get("usages") if isinstance(payload.get("usages"), dict) else {}
        for key, label in (("limit_5h", "Session (5-hour)"), ("limit_7d", "Weekly (7-day)"),
                           ("limit_month_total", "Monthly"), ("limit_month_code", "Monthly")):
            if label in seen:
                continue
            entry = usages.get(key)
            if not isinstance(entry, dict):
                continue
            add(label, finite_ratio(entry.get("used_ratio")), entry.get("reset_time"))
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
    api_bases = configured_endpoints()[0]
    tier_label = cache.get("tier") or "Kimi Code"
    status = ""
    help_text = ""
    retry = False
    limits = cache.get("limits") or []
    token, token_error = resolve_access_token()
    if token:
        fresh_limits: list[dict[str, Any]] = []
        windowless = False
        auth_failed = False
        network_failed = False
        for base in api_bases:
            payload, fetch_error = fetch_json(base + "/usages", token)
            if fetch_error == "auth":
                auth_failed = True
                continue
            if fetch_error is not None or payload is None:
                network_failed = True
                continue
            candidate = limits_from_usage_payload(payload)
            profile, _ = fetch_json(base + "/me", token)
            if isinstance(profile, dict) and str(profile.get("user_level_name") or "").strip():
                tier_label = str(profile["user_level_name"]).strip()
                cache["tier"] = tier_label
            if candidate:
                fresh_limits = candidate
                break
            windowless = True
        if fresh_limits:
            limits = fresh_limits
            cache["limits"] = limits
        elif windowless:
            # Reachable but windowless: keep cached windows and retry soon
            # rather than presenting zeros as fresh quota.
            retry = True
            if not limits and number(stats.get("totalPrompts")) <= 0:
                help_text = "Run /login in Kimi Code to restore quota."
        elif auth_failed and not network_failed:
            # Every base rejected the token: cached windows stay visible but
            # the panel must say re-login is needed. With mixed outcomes the
            # account may live on the unreachable host, so retry quietly.
            status = "Kimi Code authentication expired."
            help_text = "Run /login in Kimi Code to restore quota."
        elif network_failed or auth_failed:
            if limits:
                retry = True
            else:
                status = "Kimi Code limits unavailable."
                help_text = "Check the network connection and refresh."
                retry = True
    else:
        # No usable credential: local stats stay.
        if token_error == "network":
            # Refresh failed on the wire: keep cached windows and retry soon
            # instead of clearing them as if the login had expired.
            if limits:
                retry = True
            else:
                status = "Kimi Code limits unavailable."
                help_text = "Check the network connection and refresh."
                retry = True
        else:
            limits = []
            cache["limits"] = limits
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
                                 "database": identity, "limits": limits, "tier": tier_label})
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
