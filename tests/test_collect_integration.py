"""End-to-end collect() runs with an isolated filesystem and mocked network.

HOME/XDG_*/KIMI_CODE_HOME point at tmp dirs so no real user state is read or
written. Network is mocked per URL.
"""
import importlib.util
import json
import os
import sqlite3
import tempfile
import time
import unittest
import urllib.error
from pathlib import Path
from unittest import mock


def load(name, filename):
    path = Path(__file__).parents[1] / "collectors" / filename
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


kimi = load("kimi_collector_e2e", "kimi.py")
opencode = load("opencode_collector_e2e", "opencode.py")

NOW_MS = 1_790_000_000_000


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self):
        return json.dumps(self.payload).encode("utf-8")


class IsolatedFs(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        patches = {
            "HOME": str(root),
            "XDG_CACHE_HOME": str(root / "cache"),
            "XDG_STATE_HOME": str(root / "state"),
            "XDG_DATA_HOME": str(root / "data"),
            "KIMI_CODE_HOME": str(root / "kimi-code"),
        }
        self._patcher = mock.patch.dict(os.environ, patches)
        self._patcher.start()
        self.addCleanup(self._patcher.stop)
        self.root = root

    def write_wire(self, lines):
        wire_dir = self.root / "kimi-code" / "sessions" / "ws" / "session_abc" / "agents" / "main"
        wire_dir.mkdir(parents=True)
        path = wire_dir / "wire.jsonl"
        path.write_bytes(b"\n".join(lines) + b"\n")
        return path

    def write_credential(self, **fields):
        cred_dir = self.root / "kimi-code" / "credentials"
        cred_dir.mkdir(parents=True)
        data = {"access_token": "tok", "refresh_token": "r",
                "expires_at": int(time.time()) + 3600, "expires_in": 3600}
        data.update(fields)
        (cred_dir / "test.json").write_text(json.dumps(data), encoding="utf-8")


def wire_event(payload):
    return json.dumps(payload).encode("utf-8")


class KimiCollectTests(IsolatedFs):
    def test_no_data_no_auth_is_not_ready(self):
        record = kimi.collect(force=False, limits_only=False)
        self.assertEqual(record["id"], "kimi")
        self.assertFalse(record["ready"])
        self.assertEqual(record["limits"], [])
        self.assertNotEqual(record["authHelpText"], "")

    def test_wire_stats_flow_into_record_and_cache(self):
        self.write_wire([
            wire_event({"type": "turn.prompt", "agentId": "main", "promptId": "p1", "time": NOW_MS}),
            wire_event({"type": "usage.record", "agentId": "main", "model": "kimi-code/k3",
                        "usage": {"inputOther": 10, "output": 20}, "usageScope": "turn", "time": NOW_MS}),
        ])
        record = kimi.collect(force=False, limits_only=False)
        self.assertTrue(record["ready"])
        self.assertEqual(record["totalPrompts"], 1)
        self.assertEqual(record["modelUsage"]["k3"]["inputTokens"], 10)
        # Cache persisted: a second run reuses it incrementally.
        cache = json.loads(kimi.cache_path().read_text(encoding="utf-8"))
        self.assertEqual(cache["aggregate"]["totalPrompts"], 1)
        again = kimi.collect(force=False, limits_only=False)
        self.assertEqual(again["totalPrompts"], 1)

    def test_limits_and_tier_from_api_then_survive_outage(self):
        self.write_wire([
            wire_event({"type": "turn.prompt", "agentId": "main", "promptId": "p1", "time": NOW_MS}),
        ])
        self.write_credential()
        usages = {"usage": {"limit": "100", "used": "50", "resetTime": "2026-10-03T09:36:34Z"},
                  "limits": [], "usages": {}}
        me = {"user_level_name": "Membership"}

        def route(request, timeout=8):
            if request.full_url.endswith("/usages"):
                return FakeResponse(usages)
            return FakeResponse(me)

        with mock.patch("urllib.request.urlopen", side_effect=route):
            record = kimi.collect(force=False, limits_only=False)
        self.assertEqual(record["limits"][0]["percent"], 0.5)
        self.assertEqual(record["tierLabel"], "Membership")

        def down(request, timeout=8):
            raise urllib.error.URLError("down")

        with mock.patch("urllib.request.urlopen", side_effect=down):
            record = kimi.collect(force=True, limits_only=True)
        # Cached windows survive the outage; retry is advised.
        # (force+limits_only skips the local scan; stats come from cache.)
        self.assertEqual(record["limits"][0]["percent"], 0.5)
        self.assertTrue(record.get("retryAdvised"))
        self.assertEqual(record["tierLabel"], "Membership")


class OpencodeCollectTests(IsolatedFs):
    def make_db(self, rows):
        db_dir = self.root / "data" / "opencode"
        db_dir.mkdir(parents=True)
        path = db_dir / "opencode.db"
        connection = sqlite3.connect(path)
        connection.execute("CREATE TABLE message (id text primary key, session_id text, data text)")
        for i, data in enumerate(rows):
            connection.execute("INSERT INTO message VALUES (?, ?, ?)", (str(i), "s%d" % i, data))
        connection.commit()
        connection.close()
        return path

    def message(self, provider="opencode-go", model="opencode-go/kimi-k3"):
        return json.dumps({"role": "assistant", "providerID": provider, "modelID": model,
                           "time": {"created": NOW_MS},
                           "tokens": {"input": 10, "output": 20, "cache": {"read": 1, "write": 2}}})

    def test_local_stats_without_auth_are_neutral(self):
        self.make_db([self.message(), self.message("anthropic")])
        record = opencode.collect(force=False, limits_only=False)
        self.assertTrue(record["ready"])
        self.assertEqual(record["totalPrompts"], 1)
        self.assertEqual(record["limits"], [])
        self.assertEqual(record["usageStatusText"], "")
        self.assertEqual(record["tierLabel"], "OpenCode")

    def test_api_key_limits_flow_into_record_and_cache(self):
        self.make_db([self.message()])
        auth_dir = self.root / "data" / "opencode"
        (auth_dir / "auth.json").write_text(json.dumps({"opencode-go": "secret"}), encoding="utf-8")
        payload = {"usage": {"rolling": {"percent": 25, "resetsAt": "2026-09-25T08:00:00Z"}}}

        with mock.patch("urllib.request.urlopen", return_value=FakeResponse(payload)):
            record = opencode.collect(force=False, limits_only=False)
        self.assertEqual(record["limits"][0]["percent"], 0.25)
        self.assertEqual(record["tierLabel"], "Go")

        def down(request, timeout=5):
            raise urllib.error.URLError("down")

        with mock.patch("urllib.request.urlopen", side_effect=down):
            record = opencode.collect(force=False, limits_only=True)
        self.assertEqual(record["limits"][0]["percent"], 0.25)
        self.assertTrue(record.get("retryAdvised"))


class PurgeAndMainTests(IsolatedFs):
    def test_purge_needs_confirmation(self):
        self.assertEqual(kimi.purge(False), 2)
        self.assertEqual(opencode.purge(False), 2)

    def test_purge_removes_only_own_files(self):
        kimi_cache = kimi.cache_path()
        kimi_cache.parent.mkdir(parents=True)
        kimi_cache.write_text("{}", encoding="utf-8")
        kimi_record = kimi.record_path()
        kimi_record.parent.mkdir(parents=True)
        kimi_record.write_text("{}", encoding="utf-8")
        keeper = kimi_record.parent / "codex.json"
        keeper.write_text("{}", encoding="utf-8")
        self.assertEqual(kimi.purge(True), 0)
        self.assertFalse(kimi_cache.exists())
        self.assertFalse(kimi_record.exists())
        self.assertTrue(keeper.exists())

    def test_main_writes_record_to_state_dir(self):
        with mock.patch("sys.argv", ["kimi.py"]):
            self.assertEqual(kimi.main(), 0)
        record = json.loads(kimi.record_path().read_text(encoding="utf-8"))
        self.assertEqual(record["id"], "kimi")
        self.assertFalse(record["ready"])

    def test_opencode_main_writes_record(self):
        with mock.patch("sys.argv", ["opencode.py"]):
            self.assertEqual(opencode.main(), 0)
        record = json.loads(opencode.record_path().read_text(encoding="utf-8"))
        self.assertEqual(record["id"], "opencode")


if __name__ == "__main__":
    unittest.main()
