import importlib.util
import io
import json
import os
import stat
import tempfile
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


kimi = load("kimi_collector_fixes", "kimi.py")
opencode = load("opencode_collector_fixes", "opencode.py")


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self):
        return json.dumps(self.payload).encode("utf-8")


def http_error(code, body=b""):
    return urllib.error.HTTPError("https://x.test/token", code, "err", {}, io.BytesIO(body))


class OpencodeFreshLimitsTests(unittest.TestCase):
    def test_fresh_windows_replace_cache(self):
        fresh = [{"label": "Session (5-hour)", "percent": 0.5, "resetsAt": ""}]
        limits, help_text, retry = opencode.apply_fresh_limits(
            fresh, [{"label": "old", "percent": 0.1, "resetsAt": ""}], "e", True, {"totalPrompts": 9})
        self.assertEqual(limits, fresh)
        self.assertEqual(help_text, "")
        self.assertFalse(retry)

    def test_windowless_keeps_cache_and_retries(self):
        cached = [{"label": "Weekly (7-day)", "percent": 0.2, "resetsAt": ""}]
        limits, _, retry = opencode.apply_fresh_limits([], cached, "", False, {"totalPrompts": 9})
        self.assertEqual(limits, cached)
        self.assertTrue(retry)

    def test_error_keeps_cache_quietly(self):
        cached = [{"label": "Weekly (7-day)", "percent": 0.2, "resetsAt": ""}]
        limits, help_text, retry = opencode.apply_fresh_limits(
            None, cached, "OpenCode Go limits unavailable.", True, {"totalPrompts": 9})
        self.assertEqual(limits, cached)
        self.assertEqual(help_text, "")

    def test_error_without_cache_reports_help(self):
        limits, help_text, retry = opencode.apply_fresh_limits(
            None, [], "OpenCode Go limits unavailable.", True, {"totalPrompts": 0})
        self.assertEqual(limits, [])
        self.assertNotEqual(help_text, "")


class OpencodeLimitsParsingTests(unittest.TestCase):
    def test_nan_and_inf_percent_rejected(self):
        limits = opencode.limits_from_payload({"usage": {
            "rolling": {"percent": float("nan"), "resetsAt": ""},
            "weekly": {"percent": float("inf"), "resetsAt": ""},
            "monthly": {"percent": -5, "resetsAt": ""},
        }})
        self.assertEqual(limits, [])

    def test_non_dict_payload_is_a_retryable_error(self):
        with mock.patch("urllib.request.urlopen", return_value=FakeResponse([1, 2])):
            limits, status, retry = opencode.fetch_limits("token")
        self.assertIsNone(limits)
        self.assertTrue(retry)
        self.assertNotEqual(status, "")


class KimiOAuthTests(unittest.TestCase):
    def test_invalid_grant_detection(self):
        self.assertTrue(kimi.is_invalid_grant(http_error(400, b'{"error":"invalid_grant"}')))
        self.assertFalse(kimi.is_invalid_grant(http_error(400, b'{"error":"slow_down"}')))
        self.assertFalse(kimi.is_invalid_grant(http_error(401)))

    def test_https_validation(self):
        self.assertTrue(kimi.is_https_url("https://api.kimi.ai/coding/v1"))
        self.assertFalse(kimi.is_https_url("http://api.kimi.ai/coding/v1"))
        self.assertFalse(kimi.is_https_url("not a url"))
        self.assertFalse(kimi.is_https_url(""))

    def test_refresh_tries_every_host(self):
        good = {"access_token": "a", "refresh_token": "r", "expires_in": 3600}
        with mock.patch("urllib.request.urlopen",
                         side_effect=[http_error(401), FakeResponse(good)]):
            info, error = kimi.post_refresh_token("refresh", ["https://one.test", "https://two.test"])
        self.assertIsNotNone(info)
        self.assertIsNone(error)
        self.assertEqual(info["access_token"], "a")

    def test_all_hosts_rejecting_is_auth(self):
        with mock.patch("urllib.request.urlopen", side_effect=[http_error(401), http_error(403)]):
            info, error = kimi.post_refresh_token("refresh", ["https://one.test", "https://two.test"])
        self.assertIsNone(info)
        self.assertEqual(error, "auth")

    def test_mixed_reject_and_outage_is_network(self):
        with mock.patch("urllib.request.urlopen",
                         side_effect=[http_error(401), urllib.error.URLError("down")]):
            info, error = kimi.post_refresh_token("refresh", ["https://one.test", "https://two.test"])
        self.assertIsNone(info)
        self.assertEqual(error, "network")

    def test_revoked_grant_is_auth(self):
        with mock.patch("urllib.request.urlopen",
                         side_effect=http_error(400, b'{"error":"invalid_grant"}')):
            info, error = kimi.post_refresh_token("refresh", ["https://one.test"])
        self.assertIsNone(info)
        self.assertEqual(error, "auth")


class KimiCacheTests(unittest.TestCase):
    def test_corrupt_cache_normalises_without_raising(self):
        aggregate = kimi.normalise_aggregate({
            "totalPrompts": "3",
            "modelUsage": {"m": "not-a-bucket", "ok": {"inputTokens": 2}},
            "days": {"2026-01-01": 42, "2026-01-02": {"tokens": 5, "sessions": "x", "models": ["y"]}},
        })
        self.assertEqual(aggregate["modelUsage"], {
            "ok": {"inputTokens": 2, "outputTokens": 0, "cacheReadInputTokens": 0, "cacheCreationInputTokens": 0},
        })
        self.assertEqual(aggregate["days"]["2026-01-02"]["sessions"], [])
        self.assertEqual(aggregate["days"]["2026-01-02"]["models"], {})
        stats = kimi.stats_from_aggregate(aggregate)
        self.assertEqual(stats["totalPrompts"], 3)

    def test_number_rejects_infinity(self):
        self.assertEqual(kimi.number(float("inf")), 0)
        self.assertEqual(opencode.number(float("-inf")), 0)
        self.assertEqual(kimi.number(float("nan")), 0)

    def test_database_reuse_rules(self):
        identity = {"device": 1, "inode": 2, "size": 100, "mtime": 7}
        self.assertTrue(kimi.database_reusable(dict(identity), dict(identity)))
        grown = dict(identity, size=200)
        self.assertTrue(kimi.database_reusable(dict(identity), grown))
        self.assertFalse(kimi.database_reusable({"device": 9, "inode": 2, "size": 50}, dict(identity)))
        self.assertFalse(kimi.database_reusable({"device": 1, "inode": 2, "size": 500}, dict(identity)))

    def test_save_credential_preserves_unknown_fields(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        path = Path(directory.name) / "cred.json"
        path.write_text(json.dumps({"access_token": "old", "refresh_token": "r",
                                    "id_token": "keep-me", "expires_in": 1}), encoding="utf-8")
        token_info = {"access_token": "a", "refresh_token": "r", "expires_at": 5,
                      "expires_in": 9, "scope": "s", "token_type": "Bearer"}
        self.assertTrue(kimi.save_credential(path, token_info))
        stored = kimi.read_json(path)
        self.assertEqual(stored["id_token"], "keep-me")
        self.assertEqual(stored["access_token"], "a")
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)

    def test_read_cache_keeps_tier_string_only(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        path = Path(directory.name) / "cache.json"
        path.write_text(json.dumps({"version": 1, "tier": "Membership",
                                    "aggregate": {}, "wires": {}, "database": {}, "limits": []}),
                        encoding="utf-8")
        self.assertEqual(kimi.read_cache(path)["tier"], "Membership")


if __name__ == "__main__":
    unittest.main()
