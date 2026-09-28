import importlib.util
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest


MODULE_PATH = Path(__file__).parents[1] / "collectors" / "kimi.py"
SPEC = importlib.util.spec_from_file_location("kimi_collector", MODULE_PATH)
collector = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(collector)


def usage_record(model="kimi-code/k3-256k", created=1_790_000_000_000, tokens=None):
    return json.dumps({
        "type": "usage.record",
        "agentId": "main",
        "model": model,
        "usage": tokens or {"inputOther": 10, "output": 20, "inputCacheRead": 4, "inputCacheCreation": 5},
        "usageScope": "turn",
        "time": created,
    })


def turn_prompt(prompt_id="p1", created=1_790_000_000_000):
    return json.dumps({
        "type": "turn.prompt",
        "agentId": "main",
        "promptId": prompt_id,
        "time": created,
    })


def opencode_message(provider, created, model="kimi-for-coding", tokens=None):
    return json.dumps({
        "role": "assistant",
        "providerID": provider,
        "modelID": model,
        "time": {"created": created},
        "tokens": tokens or {"input": 1, "output": 2, "reasoning": 3, "cache": {"read": 4, "write": 5}},
    })


class KimiCollectorTests(unittest.TestCase):
    def make_wire(self, directory, lines, name="wire.jsonl"):
        root = Path(directory.name) / "ws" / "session_abc" / "agents" / "main"
        root.mkdir(parents=True)
        path = root / name
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return path

    def test_wire_counts_prompts_once_and_tokens_by_model(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        now = 1_790_000_000_000
        self.make_wire(directory, [
            turn_prompt("p1", now),
            turn_prompt("p1", now),
            usage_record("kimi-code/k3-256k", now),
            usage_record("kimi-code/k3-256k", now),
            json.dumps({"type": "interaction.request"}),
            "not-json",
        ])
        cached = {"aggregate": collector.empty_aggregate(), "wires": {}}

        aggregate, wires, rebuilt = collector.scan_wires(Path(directory.name), cached, force=False)

        self.assertTrue(rebuilt)
        self.assertEqual(aggregate["totalPrompts"], 1)
        self.assertEqual(aggregate["modelUsage"]["k3-256k"]["inputTokens"], 20)
        self.assertEqual(aggregate["modelUsage"]["k3-256k"]["outputTokens"], 40)
        self.assertEqual(aggregate["modelUsage"]["k3-256k"]["cacheReadInputTokens"], 8)
        self.assertEqual(len(aggregate["sessionIds"]), 1)
        self.assertIn(str(list(wires)[0]), wires)

    def test_wire_scan_is_incremental(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        now = 1_790_000_000_000
        path = self.make_wire(directory, [turn_prompt("p1", now), usage_record("kimi-code/k3-256k", now)])
        cached = {"aggregate": collector.empty_aggregate(), "wires": {}}
        aggregate, wires, _ = collector.scan_wires(Path(directory.name), cached, force=False)
        with open(path, "a", encoding="utf-8") as stream:
            stream.write(turn_prompt("p2", now) + "\n")
            stream.write(usage_record("kimi-code/k3-256k", now) + "\n")

        aggregate, wires, rebuilt = collector.scan_wires(
            Path(directory.name), {"aggregate": aggregate, "wires": wires}, force=False)

        self.assertFalse(rebuilt)
        self.assertEqual(aggregate["totalPrompts"], 2)
        self.assertEqual(aggregate["modelUsage"]["k3-256k"]["inputTokens"], 20)

    def test_wire_rebuild_after_rotation(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        now = 1_790_000_000_000
        path = self.make_wire(directory, [turn_prompt("p1", now), usage_record("kimi-code/k3-256k", now)])
        cached = {"aggregate": collector.empty_aggregate(), "wires": {}}
        aggregate, wires, _ = collector.scan_wires(Path(directory.name), cached, force=False)
        path.write_text(turn_prompt("p9", now) + "\n", encoding="utf-8")

        aggregate, _, rebuilt = collector.scan_wires(
            Path(directory.name), {"aggregate": aggregate, "wires": wires}, force=False)

        self.assertTrue(rebuilt)
        self.assertEqual(aggregate["totalPrompts"], 1)

    def test_opencode_scan_counts_only_kimi_providers(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        path = Path(directory.name) / "opencode.db"
        connection = sqlite3.connect(path)
        connection.execute("CREATE TABLE message (id text primary key, session_id text, time_created integer, time_updated integer, data text)")
        now = 1_790_000_000_000
        connection.execute("INSERT INTO message VALUES (?, ?, ?, ?, ?)", ("1", "s1", now, now, opencode_message("kimi-for-coding", now)))
        connection.execute("INSERT INTO message VALUES (?, ?, ?, ?, ?)", ("2", "s2", now, now, opencode_message("moonshot", now)))
        connection.execute("INSERT INTO message VALUES (?, ?, ?, ?, ?)", ("3", "s3", now, now, opencode_message("anthropic", now)))
        connection.execute("INSERT INTO message VALUES (?, ?, ?, ?, ?)", ("4", "bad", now, now, "not-json"))
        connection.commit()

        aggregate, last_row_id = collector.scan_opencode_database(
            path, collector.empty_aggregate(), {"database": {}}, rebuilt=True, force=False)

        self.assertEqual(len(aggregate["sessionIds"]), 2)
        self.assertEqual(aggregate["modelUsage"]["kimi-for-coding"]["outputTokens"], 10)
        self.assertGreater(last_row_id, 0)

    def test_opencode_scan_is_incremental(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        path = Path(directory.name) / "opencode.db"
        connection = sqlite3.connect(path)
        connection.execute("CREATE TABLE message (id text primary key, session_id text, time_created integer, time_updated integer, data text)")
        now = 1_790_000_000_000
        connection.execute("INSERT INTO message VALUES (?, ?, ?, ?, ?)", ("1", "s1", now, now, opencode_message("moonshot", now)))
        connection.commit()
        aggregate, last_row_id = collector.scan_opencode_database(
            path, collector.empty_aggregate(), {"database": {}}, rebuilt=True, force=False)
        identity = collector.database_identity(path)
        identity["lastRowId"] = last_row_id
        connection.execute("INSERT INTO message VALUES (?, ?, ?, ?, ?)", ("2", "s2", now, now, opencode_message("moonshot", now)))
        connection.commit()

        aggregate, _ = collector.scan_opencode_database(
            path, aggregate, {"database": identity}, rebuilt=False, force=False)

        self.assertEqual(len(aggregate["sessionIds"]), 2)
        self.assertEqual(aggregate["modelUsage"]["kimi-for-coding"]["inputTokens"], 2)

    def test_limits_prefer_enforced_quota_over_display_ratios(self):
        limits = collector.limits_from_usage_payload({
            "usage": {"limit": "100", "used": "100", "resetTime": "2026-10-03T09:36:34.494238Z"},
            "limits": [{"window": {"duration": 300, "timeUnit": "TIME_UNIT_MINUTE"},
                        "detail": {"limit": "100", "remaining": "100", "resetTime": "2026-09-28T21:36:34.494238Z"}}],
            "usages": {
                "limit_5h": {"used_ratio": 0, "reset_time": "2026-09-28T21:36:34Z"},
                "limit_7d": {"used_ratio": 0, "reset_time": "2026-10-03T09:36:34Z"},
            },
        })

        self.assertEqual([item["label"] for item in limits], ["Weekly (7-day)", "Session (5-hour)"])
        self.assertEqual(limits[0]["percent"], 1.0)
        self.assertEqual(limits[0]["resetsAt"], "2026-10-03T09:36:34.494238Z")
        self.assertEqual(limits[1]["percent"], 0.0)

    def test_limits_read_used_when_present(self):
        limits = collector.limits_from_usage_payload({
            "usage": {"limit": "100", "used": "12", "resetTime": "2026-10-03T09:36:34Z"},
            "limits": [{"window": {"duration": 300, "timeUnit": "TIME_UNIT_MINUTE"},
                        "detail": {"limit": "100", "used": "47", "resetTime": "2026-09-28T16:36:34Z"}}],
            "usages": {},
        })

        self.assertEqual(limits[0]["percent"], 0.12)
        self.assertEqual(limits[1]["percent"], 0.47)

    def test_limits_fall_back_to_display_ratios_only(self):
        limits = collector.limits_from_usage_payload({"usages": {
            "limit_5h": {"used_ratio": 0.47, "reset_time": "2026-09-28T16:36:34Z"},
            "limit_7d": {"used_ratio": 0.12, "reset_time": "2026-10-03T09:36:34Z"},
        }})

        self.assertEqual([item["label"] for item in limits], ["Session (5-hour)", "Weekly (7-day)"])
        self.assertEqual(limits[0]["percent"], 0.47)
        self.assertEqual(limits[1]["resetsAt"], "2026-10-03T09:36:34Z")

    def test_limits_empty_payload_yields_no_windows(self):
        self.assertEqual(collector.limits_from_usage_payload({}), [])
        self.assertEqual(collector.limits_from_usage_payload({"usages": {}}), [])

    def test_window_labels_only_known_durations(self):
        self.assertEqual(collector.window_label_for_duration(300, "TIME_UNIT_MINUTE"), "Session (5-hour)")
        self.assertEqual(collector.window_label_for_duration(10080, "TIME_UNIT_MINUTE"), "Weekly (7-day)")
        self.assertEqual(collector.window_label_for_duration(43200, "TIME_UNIT_MINUTE"), "Monthly")
        self.assertEqual(collector.window_label_for_duration(18000, "TIME_UNIT_SECOND"), "Session (5-hour)")
        self.assertEqual(collector.window_label_for_duration(604800, "TIME_UNIT_SECOND"), "Weekly (7-day)")
        # Unknown durations are skipped, never mislabelled.
        self.assertEqual(collector.window_label_for_duration(60, "TIME_UNIT_MINUTE"), "")
        self.assertEqual(collector.window_label_for_duration(300, "TIME_UNIT_SECOND"), "")
        self.assertEqual(collector.window_label_for_duration(300, "furlongs"), "")
        self.assertEqual(collector.window_label_for_duration("nan", "TIME_UNIT_MINUTE"), "")

    def test_unknown_durations_are_skipped(self):
        limits = collector.limits_from_usage_payload({
            "limits": [{"window": {"duration": 60, "timeUnit": "TIME_UNIT_MINUTE"},
                        "detail": {"limit": "100", "used": "99", "resetTime": "2026-09-28T16:36:34Z"}}],
            "usages": {},
        })

        self.assertEqual(limits, [])

    def test_ratios_reject_nonfinite_values(self):
        self.assertIsNone(collector.ratio_used("inf", "10"))
        self.assertIsNone(collector.ratio_used(100, "nan"))
        self.assertIsNone(collector.ratio_used(0, 0))
        self.assertIsNone(collector.finite_ratio(float("inf")))
        self.assertIsNone(collector.finite_ratio(float("nan")))
        self.assertEqual(collector.ratio_used("100", "100"), 1.0)
        limits = collector.limits_from_usage_payload({
            "usage": {"limit": "inf", "used": "10", "resetTime": "2026-10-03T09:36:34Z"},
            "usages": {},
        })

        self.assertEqual(limits, [])

    def test_authoritative_windows_win_and_display_ratios_fill_gaps(self):
        limits = collector.limits_from_usage_payload({
            "usage": {"limit": "100", "used": "80", "resetTime": "2026-10-03T09:36:34Z"},
            "limits": [],
            "usages": {
                "limit_5h": {"used_ratio": 0.47, "reset_time": "2026-09-28T16:36:34Z"},
                "limit_7d": {"used_ratio": 0.12, "reset_time": "2026-10-03T09:36:34Z"},
            },
        })

        self.assertEqual([item["label"] for item in limits], ["Weekly (7-day)", "Session (5-hour)"])
        # Weekly comes from the authoritative pool, not the display ratio.
        self.assertEqual(limits[0]["percent"], 0.8)
        self.assertEqual(limits[1]["percent"], 0.47)

    def test_credential_needs_refresh_uses_cli_threshold(self):
        import time as time_module

        fresh = {"access_token": "x", "expires_at": time_module.time() + 800, "expires_in": 900}
        stale = {"access_token": "x", "expires_at": time_module.time() + 400, "expires_in": 900}
        missing = {"access_token": "", "expires_at": 0, "expires_in": 900}
        # Threshold for a 900s lifetime is max(300, 450) = 450s.
        self.assertFalse(collector.credential_needs_refresh(fresh))
        self.assertTrue(collector.credential_needs_refresh(stale))
        self.assertTrue(collector.credential_needs_refresh(missing))

    def test_credential_path_rejects_traversal(self):
        self.assertIsNone(collector.credential_path_for_key(""))
        self.assertIsNone(collector.credential_path_for_key("../evil"))
        self.assertIsNone(collector.credential_path_for_key(".hidden"))
        path = collector.credential_path_for_key("oauth/kimi-code-env-abc")
        self.assertIsNotNone(path)
        self.assertTrue(str(path).endswith("credentials/kimi-code-env-abc.json"))

    def test_save_credential_roundtrip_is_0600(self):
        import os
        import stat

        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        path = Path(directory.name) / "cred.json"
        token_info = {"access_token": "a", "refresh_token": "r", "expires_at": 123,
                      "expires_in": 900, "scope": "kimi-code", "token_type": "Bearer"}

        self.assertTrue(collector.save_credential(path, token_info))

        stored = collector.read_json(path)
        self.assertEqual(stored["refresh_token"], "r")
        self.assertEqual(stored["expires_at"], 123)
        mode = stat.S_IMODE(os.stat(path).st_mode)
        self.assertEqual(mode, 0o600)

    def test_record_without_auth_is_neutral_when_local_usage_exists(self):
        record = collector.base_record({"totalPrompts": 3}, [], "Kimi Code")

        self.assertTrue(record["ready"])
        self.assertEqual(record["usageStatusText"], "")
        self.assertEqual(record["authHelpText"], "")


if __name__ == "__main__":
    unittest.main()
