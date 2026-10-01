"""Run the pure QML/JS helpers from Panel.qml and Main.qml under node.

The functions are extracted from the real sources (brace-matched, no copies),
so this test fails if the shipped logic regresses. Skipped when node is absent.
"""
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).parents[1]

PANEL_FUNCTIONS = ["windowIsLong", "windowSpanMs", "labelTalksAboutTime", "windowTitle", "limitWindow",
                   "currencyPrefix", "formatMoney"]
MAIN_FUNCTIONS = ["numberValue", "updateRank", "mergeUpdateAgentIds", "combineNumber", "formatTokenCount",
                   "collectorId", "authHelpTextForRecord"]

CHECKS = r"""
check("session spelled out", windowTitle("Session (5-hour)") === "Session");
check("abbreviated hours", windowTitle("5h window") === "Session");
check("abbreviated minutes", windowTitle("30m window") === "Session");
check("hours plural", windowTitle("5hours rolling") === "Session");
check("weekly", windowTitle("Weekly (7-day)") === "Weekly");
check("monthly", windowTitle("Monthly") === "Monthly");
check("model size is not a session", windowTitle("Opus 5 (1M context)") === "Opus 5");
check("bare model id is not a session", windowTitle("claude-opus-4-8") !== "Session");
check("empty label", windowTitle("") === "Limit");
check("explicit title wins", limitWindow("Opus 5 (1M context)", 0.5, "", "Opus 5").title === "Opus 5");
check("rank force", updateRank("force") === 2);
check("rank normal", updateRank("normal") === 1);
check("rank limits", updateRank("limits") === 0);
check("merge full wins", mergeUpdateAgentIds(["a"], null) === null);
check("merge union", JSON.stringify(mergeUpdateAgentIds(["a", "b"], ["b", "c"])) === '["a","b","c"]');
check("combine sum", combineNumber(true, 2, 3) === 5);
check("combine max", combineNumber(false, 2, 3) === 3);
check("combine stale max", combineNumber(false, 5, 3) === 5);
check("number garbage", numberValue("fast") === 0);
check("number clamps negatives", numberValue(-3) === 0);
check("tokens round up to mega", formatTokenCount(999999) === "1.0M");
check("tokens kilo", formatTokenCount(1500) === "1.5K");
check("tokens zero", formatTokenCount(0) === "0");
check("tokens null", formatTokenCount(null) === "0");
check("tokens garbage", formatTokenCount("fast") === "0");
check("money usd", formatMoney(10, "USD") === "$10.00");
check("money jpy rounds", formatMoney(10.5, "JPY") === "JPY 11");
check("collector id", collectorId("/x/kimi.py") === "kimi");
check("collector id rejects non-py", collectorId("/x/notes.txt") === "");
check("stale login hint dropped with live limits", authHelpTextForRecord({usageStatusText: "", authHelpText: "Run `codex login` to authenticate.", limits: [{label: "5h"}]}) === "");
check("help kept when status set", authHelpTextForRecord({usageStatusText: "Sign-in expired", authHelpText: "expired", limits: [{label: "5h"}]}) === "expired");
check("help kept without limits", authHelpTextForRecord({usageStatusText: "", authHelpText: "Run `codex login` to authenticate.", limits: []}) === "Run `codex login` to authenticate.");
check("empty help stays empty", authHelpTextForRecord({usageStatusText: "", authHelpText: "", limits: [{label: "5h"}]}) === "");
"""


def extract(source, name):
    start = source.index("function " + name + "(")
    brace = source.index("{", start)
    depth = 0
    for i in range(brace, len(source)):
        if source[i] == "{":
            depth += 1
        elif source[i] == "}":
            depth -= 1
            if depth == 0:
                return source[start:i + 1]
    raise AssertionError("unbalanced " + name)


class QmlLogicTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("node"), "node not available")
    def test_pure_helpers(self):
        panel = (ROOT / "Panel.qml").read_text(encoding="utf-8")
        main = (ROOT / "Main.qml").read_text(encoding="utf-8")
        parts = [extract(panel, name) for name in PANEL_FUNCTIONS]
        parts += [extract(main, name) for name in MAIN_FUNCTIONS]
        script = "\n".join(parts) + """
let failures = 0;
function check(name, ok) { if (!ok) { console.error("FAIL " + name); failures++; } }
%s
if (failures > 0) { process.exit(1); }
console.log("qml logic ok");
""" % CHECKS
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "qml_logic.mjs"
            path.write_text(script, encoding="utf-8")
            result = subprocess.run(["node", str(path)], capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)


if __name__ == "__main__":
    unittest.main()
