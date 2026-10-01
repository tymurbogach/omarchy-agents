#!/usr/bin/env python3
"""Collect Codex usage with bounded retries for its app-server RPC.

Omarchy owns the Codex session scanner. This wrapper keeps that scanner as the
source of local statistics, retries its short-lived RPC startup failures, and
writes the panel record atomically.
"""

from __future__ import annotations

import argparse
import copy
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
from typing import Any


AGENT_ID = "codex"
AGENT_NAME = "Codex"
COMMAND = "omarchy-agent-usage-codex"
TRANSIENT_STATUS = "Codex limits unavailable"
RETRY_DELAYS = (0, 1, 2)
KNOWN_STEPS = ("initialize", "account/read", "account/rateLimits/read")
RETRY_DIAGNOSTIC = "Codex did not respond after 3 attempts. Retrying in 30 seconds."


def state_home() -> Path:
    return Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state")


def record_path() -> Path:
    return state_home() / "omarchy" / "agents" / "usage" / "codex.json"


def base_record() -> dict[str, Any]:
    return {
        "schemaVersion": 1,
        "id": AGENT_ID,
        "name": AGENT_NAME,
        "ready": False,
        "hasLocalStats": False,
        "limits": [],
        "usageStatusText": "Codex unavailable",
        "authHelpText": "Codex is not installed.",
    }


def atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, separators=(",", ":"))
            stream.write("\n")
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def command_available() -> bool:
    return shutil.which(COMMAND) is not None


def command_args(force: bool, limits_only: bool) -> list[str]:
    command = [COMMAND]
    if force:
        command.append("--force")
    if limits_only:
        command.append("--limits-only")
    return command


def run_once(force: bool, limits_only: bool) -> dict[str, Any] | None:
    try:
        result = subprocess.run(
            command_args(force, limits_only), capture_output=True, text=True, timeout=30, check=False
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    try:
        record = json.loads(result.stdout)
    except json.JSONDecodeError:
        return None
    return record if isinstance(record, dict) else None


def known_step(record: dict[str, Any]) -> str:
    text = str(record.get("authHelpText") or "")
    return next((step for step in KNOWN_STEPS if step in text), "")


def successful(record: dict[str, Any]) -> bool:
    return str(record.get("usageStatusText") or "") == ""


def normalise_success(record: dict[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(record)
    result["schemaVersion"] = 1
    result["id"] = AGENT_ID
    result["name"] = str(result.get("name") or AGENT_NAME)
    result["usageStatusText"] = ""
    # The installed collector initializes this before the RPC reply. A clean
    # reply therefore proves the login hint is stale, even with no limits.
    if str(result.get("authHelpText") or "") == "Run `codex login` to authenticate.":
        result["authHelpText"] = ""
    result.pop("retryAdvised", None)
    return result


def failed_record(record: dict[str, Any] | None) -> dict[str, Any]:
    result = copy.deepcopy(record) if record else base_record()
    step = known_step(result)
    result["schemaVersion"] = 1
    result["id"] = AGENT_ID
    result["name"] = str(result.get("name") or AGENT_NAME)
    result["limits"] = result.get("limits") if isinstance(result.get("limits"), list) else []
    result["usageStatusText"] = RETRY_DIAGNOSTIC
    result["authHelpText"] = "The request failed at " + step + "." if step else ""
    result["retryAdvised"] = True
    return result


def unavailable_record(record: dict[str, Any] | None) -> dict[str, Any]:
    result = copy.deepcopy(record) if record else base_record()
    result["schemaVersion"] = 1
    result["id"] = AGENT_ID
    result["name"] = str(result.get("name") or AGENT_NAME)
    result["limits"] = result.get("limits") if isinstance(result.get("limits"), list) else []
    result["usageStatusText"] = "Codex unavailable"
    result["authHelpText"] = "Codex could not start."
    result.pop("retryAdvised", None)
    return result


def collect(force: bool = False, limits_only: bool = False) -> dict[str, Any]:
    if not command_available():
        return base_record()

    last_record: dict[str, Any] | None = None
    for attempt, delay in enumerate(RETRY_DELAYS):
        if delay:
            time.sleep(delay)
        record = run_once(force, limits_only)
        if record is not None:
            last_record = record
            if successful(record):
                return normalise_success(record)
            # Authentication and installation failures cannot recover through
            # a second app-server launch. Only the known transient status does.
            if str(record.get("usageStatusText") or "") != TRANSIENT_STATUS:
                return unavailable_record(record)
    return failed_record(last_record)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--limits-only", action="store_true")
    args = parser.parse_args()
    atomic_json(record_path(), collect(args.force, args.limits_only))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
