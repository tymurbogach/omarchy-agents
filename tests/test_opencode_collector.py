import importlib.util
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest


MODULE_PATH = Path(__file__).parents[1] / "collectors" / "opencode.py"
SPEC = importlib.util.spec_from_file_location("opencode_collector", MODULE_PATH)
collector = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(collector)


def message(provider, created, model="opencode-go/kimi-k3", tokens=None):
    return json.dumps({
        "role": "assistant",
        "providerID": provider,
        "modelID": model,
        "time": {"created": created},
        "tokens": tokens or {"input": 10, "output": 20, "reasoning": 3, "cache": {"read": 4, "write": 5}},
    })


class OpenCodeCollectorTests(unittest.TestCase):
    def make_database(self):
        directory = tempfile.TemporaryDirectory()
        path = Path(directory.name) / "opencode.db"
        connection = sqlite3.connect(path)
        connection.execute("CREATE TABLE message (id text primary key, session_id text, time_created integer, time_updated integer, data text)")
        return directory, path, connection

    def test_scan_counts_only_open_code_go_messages(self):
        directory, path, connection = self.make_database()
        self.addCleanup(directory.cleanup)
        now = 1_790_000_000_000
        connection.execute("INSERT INTO message VALUES (?, ?, ?, ?, ?)", ("1", "go-session", now, now, message("opencode-go", now)))
        connection.execute("INSERT INTO message VALUES (?, ?, ?, ?, ?)", ("2", "zen-session", now, now, message("opencode", now, model="opencode/muse-spark-1.3")))
        connection.execute("INSERT INTO message VALUES (?, ?, ?, ?, ?)", ("3", "claude-session", now, now, message("anthropic", now)))
        connection.execute("INSERT INTO message VALUES (?, ?, ?, ?, ?)", ("4", "bad", now, now, "not-json"))
        connection.commit()
        cached = {"aggregate": collector.empty_aggregate(), "database": {}, "lastRowId": 0}

        aggregate, last_row_id, _ = collector.scan_database(path, cached, force=False)

        self.assertEqual(aggregate["totalPrompts"], 2)
        self.assertEqual(len(aggregate["sessionIds"]), 2)
        self.assertEqual(aggregate["modelUsage"]["kimi-k3"]["outputTokens"], 23)
        self.assertGreater(last_row_id, 0)

    def test_incremental_scan_does_not_recount_prior_rows(self):
        directory, path, connection = self.make_database()
        self.addCleanup(directory.cleanup)
        now = 1_790_000_000_000
        connection.execute("INSERT INTO message VALUES (?, ?, ?, ?, ?)", ("1", "one", now, now, message("opencode-go", now)))
        connection.commit()
        cached = {"aggregate": collector.empty_aggregate(), "database": {}, "lastRowId": 0}
        aggregate, last_row_id, identity = collector.scan_database(path, cached, force=False)
        connection.execute("INSERT INTO message VALUES (?, ?, ?, ?, ?)", ("2", "two", now, now, message("opencode-go", now, tokens={"input": 1, "output": 2, "cache": {}})))
        connection.commit()

        aggregate, last_row_id, _ = collector.scan_database(path, {"aggregate": aggregate, "database": identity, "lastRowId": last_row_id}, force=False)

        self.assertEqual(aggregate["totalPrompts"], 2)
        self.assertEqual(aggregate["modelUsage"]["kimi-k3"]["inputTokens"], 11)
        self.assertGreater(last_row_id, 1)

    def test_limits_use_the_generic_panel_shape(self):
        limits = collector.limits_from_payload({"usage": {
            "rolling": {"percent": 12.5, "resetsAt": "2026-09-25T08:00:00Z"},
            "weekly": {"percent": 50, "resetsAt": "2026-09-29T00:00:00Z"},
            "monthly": {"percent": 99.5, "resetsAt": "2026-10-01T00:00:00Z"},
        }})

        self.assertEqual([item["label"] for item in limits], ["Session (5-hour)", "Weekly (7-day)", "Monthly"])
        self.assertEqual(limits[0]["percent"], 0.125)
        self.assertEqual(limits[2]["percent"], 0.995)

    def test_record_without_go_auth_is_neutral_when_local_usage_exists(self):
        record = collector.base_record({"totalPrompts": 1}, [], "OpenCode")

        self.assertTrue(record["ready"])
        self.assertEqual(record["usageStatusText"], "")
        self.assertEqual(record["authHelpText"], "")

    def test_console_account_meters_use_the_generic_panel_shape(self):
        limits = collector.limits_from_console_payload({"access": {"meters": {
            "fiveHour": {"usedMicroCents": "589808070", "limitMicroCents": "1200000000", "resetsAt": "2026-09-25T06:27:06.715Z"},
            "week": {"usedMicroCents": "976390792", "limitMicroCents": "3000000000", "resetsAt": "2026-09-28T00:00:00.000Z"},
            "month": {"usedMicroCents": "976390792", "limitMicroCents": "6000000000"},
        }}})

        self.assertEqual([item["label"] for item in limits], ["Session (5-hour)", "Weekly (7-day)", "Monthly"])
        self.assertAlmostEqual(limits[0]["percent"], 589808070 / 1200000000)
        self.assertEqual(limits[2]["resetsAt"], "")

    def test_oauth_session_reads_active_account_without_exposing_tokens(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        path = Path(directory.name) / "opencode.db"
        connection = sqlite3.connect(path)
        connection.executescript("""
            CREATE TABLE account (id text primary key, access_token text not null);
            CREATE TABLE account_state (active_account_id text, active_org_id text);
        """)
        connection.execute("INSERT INTO account VALUES (?, ?)", ("user", "secret"))
        connection.execute("INSERT INTO account_state VALUES (?, ?)", ("user", "org_test"))
        connection.commit()
        connection.close()

        self.assertEqual(collector.oauth_session(path), ("secret", "org_test"))


if __name__ == "__main__":
    unittest.main()
