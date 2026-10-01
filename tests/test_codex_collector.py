"""Tests for the retry wrapper around Omarchy's Codex collector."""
import importlib.util
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


MODULE_PATH = Path(__file__).parents[1] / "collectors" / "codex.py"
SPEC = importlib.util.spec_from_file_location("codex_collector", MODULE_PATH)
collector = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(collector)


def record(status="", help_text="Run `codex login` to authenticate.", limits=None):
    return {
        "schemaVersion": 1, "id": "codex", "name": "Codex", "ready": True,
        "totalPrompts": 7, "limits": [] if limits is None else limits,
        "usageStatusText": status, "authHelpText": help_text,
    }


class CodexCollectorTests(unittest.TestCase):
    def test_first_attempt_success_clears_stale_login_hint_without_limits(self):
        with mock.patch.object(collector, "command_available", return_value=True), \
             mock.patch.object(collector, "run_once", return_value=record() ) as run:
            result = collector.collect()
        self.assertEqual(run.call_count, 1)
        self.assertEqual(result["usageStatusText"], "")
        self.assertEqual(result["authHelpText"], "")
        self.assertEqual(result["limits"], [])
        self.assertNotIn("retryAdvised", result)

    def test_account_read_failure_retries_then_succeeds(self):
        failed = record(collector.TRANSIENT_STATUS, "timeout during account/read: /secret/path")
        succeeded = record("", "", [{"label": "Session", "percent": 0.2}])
        with mock.patch.object(collector, "command_available", return_value=True), \
             mock.patch.object(collector, "run_once", side_effect=[failed, succeeded]) as run, \
             mock.patch.object(collector.time, "sleep") as sleep:
            result = collector.collect()
        self.assertEqual(run.call_count, 2)
        sleep.assert_called_once_with(1)
        self.assertEqual(result["limits"], succeeded["limits"])
        self.assertEqual(result["usageStatusText"], "")

    def test_three_transient_failures_preserve_stats_and_advise_retry(self):
        failed = record(collector.TRANSIENT_STATUS, "failed at account/rateLimits/read: token=secret")
        with mock.patch.object(collector, "command_available", return_value=True), \
             mock.patch.object(collector, "run_once", return_value=failed) as run, \
             mock.patch.object(collector.time, "sleep") as sleep:
            result = collector.collect()
        self.assertEqual(run.call_count, 3)
        self.assertEqual(sleep.call_args_list, [mock.call(1), mock.call(2)])
        self.assertEqual(result["totalPrompts"], 7)
        self.assertEqual(result["usageStatusText"], collector.RETRY_DIAGNOSTIC)
        self.assertEqual(result["authHelpText"], "The request failed at account/rateLimits/read.")
        self.assertTrue(result["retryAdvised"])
        self.assertNotIn("secret", json.dumps(result))
        self.assertNotIn("/secret/path", json.dumps(result))

    def test_missing_command_does_not_retry(self):
        with mock.patch.object(collector, "command_available", return_value=False), \
             mock.patch.object(collector, "run_once") as run:
            result = collector.collect()
        run.assert_not_called()
        self.assertEqual(result["usageStatusText"], "Codex unavailable")
        self.assertFalse(result.get("retryAdvised", False))

    def test_forwards_force_and_limits_only(self):
        self.assertEqual(collector.command_args(True, False), [collector.COMMAND, "--force"])
        self.assertEqual(collector.command_args(False, True), [collector.COMMAND, "--limits-only"])
        self.assertEqual(collector.command_args(True, True), [collector.COMMAND, "--force", "--limits-only"])

    def test_main_writes_atomic_record_contract(self):
        with tempfile.TemporaryDirectory() as directory, \
             mock.patch.dict(os.environ, {"XDG_STATE_HOME": directory}), \
             mock.patch.object(collector, "collect", return_value=record("", "")), \
             mock.patch.object(sys, "argv", ["codex.py"]):
            self.assertEqual(collector.main(), 0)
            path = collector.record_path()
            stored = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(stored["id"], "codex")
            self.assertEqual(stored["schemaVersion"], 1)
            self.assertIsInstance(stored["limits"], list)
            self.assertEqual(list(path.parent.glob("codex.json.*.tmp")), [])
