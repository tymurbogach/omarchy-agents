"""Run Main.qml's sync aggregation under node with a Quickshell stub.

Covers the semantics TASKS.md promises: per-device last-wins dedupe, the 48h
freshness window, today-only-from-today, and account-scope max merge.
Skipped when node is absent.
"""
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from test_qml_logic import extract


ROOT = Path(__file__).parents[1]

SYNC_FUNCTIONS = ["snapshotMs", "dateString", "recentDateStrings", "emptyTokenBucket",
                  "combineNumber", "combineObjectNumbers", "numberValue", "safeDeviceId",
                  "aggregateSnapshots"]

PRELUDE = r"""
var Quickshell = { env: function(key) { return ""; } };
var root = { detectedHostname: "testhost" };
var syncFreshMs = 48 * 3600 * 1000;
"""

CHECKS = r"""
var dates = recentDateStrings();
var today = dates[dates.length - 1];
var nowMs = Date.now();
var H = 3600000;
function devStats(o) {
  o = o || {};
  return {
    providerName: "Codex", ready: true, hasLocalStats: true, hasPromptStats: true,
    scope: o.scope || "device",
    todayPrompts: o.todayPrompts || 0, todaySessions: o.todaySessions || 0,
    todayTotalTokens: o.todayTotalTokens || 0, todayTokensByModel: {},
    recentDays: [], totalPrompts: o.totalPrompts || 0, totalSessions: o.totalSessions || 0,
    activeDays: 0, activeDates: [], modelUsage: {}
  };
}
function snap(deviceId, ageMs, providers) {
  return { deviceId: deviceId, updatedAtMs: nowMs - ageMs, providers: providers };
}
var p1 = {}; p1.codex = devStats({todayPrompts: 5, totalPrompts: 5});
var p2 = {}; p2.codex = devStats({todayPrompts: 7, totalPrompts: 7});
var agg = aggregateSnapshots([snap("lap", 2000, p1), snap("lap", 1000, p2)]);
check("dedupe last wins", agg.providers.codex.todayPrompts === 7);
check("dedupe totals last wins", agg.providers.codex.totalPrompts === 7);
var ps = {}; ps.codex = devStats({todayPrompts: 100, totalPrompts: 50});
var aggS = aggregateSnapshots([snap("old", 49 * H, ps)]);
check("stale skips today", aggS.providers.codex.todayPrompts === 0);
check("stale keeps totals", aggS.providers.codex.totalPrompts === 50);
var py = {}; py.codex = devStats({todayPrompts: 9, totalPrompts: 11});
var aggY = aggregateSnapshots([snap("lap", 25 * H, py)]);
check("yesterday skips today", aggY.providers.codex.todayPrompts === 0);
check("yesterday keeps totals", aggY.providers.codex.totalPrompts === 11);
var pa = {}; pa.fire = devStats({scope: "account", todayTotalTokens: 100, totalPrompts: 100});
var pb = {}; pb.fire = devStats({scope: "account", todayTotalTokens: 200, totalPrompts: 200});
var pc = {}; pc.fire = devStats({scope: "account", todayTotalTokens: 999, totalPrompts: 999});
var aggA = aggregateSnapshots([snap("a", 1000, pa), snap("b", 2000, pb), snap("c", 49 * H, pc)]);
check("account takes max", aggA.providers.fire.todayTotalTokens === 200);
check("stale account ignored", aggA.providers.fire.totalPrompts === 200);
var p3 = {}; p3.codex = devStats({todayPrompts: 3});
var p4 = {}; p4.codex = devStats({todayPrompts: 4});
var aggD = aggregateSnapshots([snap("a", 1000, p3), snap("b", 1000, p4)]);
check("device adds up", aggD.providers.codex.todayPrompts === 7);
check("device count", aggD.providers.codex.deviceCount === 2);
check("empty stays empty", aggregateSnapshots([]).deviceCount === 0);
"""


class SyncAggregationTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("node"), "node not available")
    def test_aggregate_snapshots(self):
        main = (ROOT / "Main.qml").read_text(encoding="utf-8")
        parts = [extract(main, name) for name in SYNC_FUNCTIONS]
        script = PRELUDE + "\n".join(parts) + """
let failures = 0;
function check(name, ok) { if (!ok) { console.error("FAIL " + name); failures++; } }
%s
if (failures > 0) { process.exit(1); }
console.log("sync aggregation ok");
""" % CHECKS
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "sync_aggregation.mjs"
            path.write_text(script, encoding="utf-8")
            result = subprocess.run(["node", str(path)], capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)


if __name__ == "__main__":
    unittest.main()
