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
                   "limitWindows", "currencyPrefix", "formatMoney", "recordAgeText"]
MAIN_FUNCTIONS = ["numberValue", "updateRank", "mergeUpdateAgentIds", "combineNumber", "formatTokenCount",
                   "collectorId", "hasLocalCollector", "updateCommand", "authHelpTextForRecord", "recordUpdatedMs"]

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
let collectorPaths = ["/x/kimi.py"];
let settings = {providers: {}};
check("codex stays in Omarchy update without local collector", updateCommand("normal", null).indexOf("codex") === -1);
collectorPaths = ["/x/codex.py"];
let codexAt = updateCommand("normal", null).indexOf("codex");
check("codex excluded with local collector", codexAt > 0 && updateCommand("normal", null)[codexAt - 1] === "--except");
check("stale login hint dropped with live limits", authHelpTextForRecord({usageStatusText: "", authHelpText: "Run `codex login` to authenticate.", limits: [{label: "5h"}]}) === "");
check("help kept when status set", authHelpTextForRecord({usageStatusText: "Sign-in expired", authHelpText: "expired", limits: [{label: "5h"}]}) === "expired");
check("help kept without limits", authHelpTextForRecord({usageStatusText: "", authHelpText: "Run `codex login` to authenticate.", limits: []}) === "Run `codex login` to authenticate.");
check("empty help stays empty", authHelpTextForRecord({usageStatusText: "", authHelpText: "", limits: [{label: "5h"}]}) === "");
check("codex golden drops stale hint", authHelpTextForRecord({usageStatusText: "", authHelpText: "Run `codex login` to authenticate.", limits: [{label: "5h window", percent: 0, resetsAt: "2026-10-01T18:17:02+00:00"}, {label: "Weekly (7-day)", percent: 0.45, resetsAt: "2026-10-04T04:40:04+00:00"}], tierLabel: "plus"}) === "");
check("limits null keeps help", authHelpTextForRecord({usageStatusText: "", authHelpText: "Run `codex login` to authenticate.", limits: null}) === "Run `codex login` to authenticate.");
check("infinite percent dropped", limitWindows({limits: [{label: "5h", percent: 1/0}]}).length === 0);
check("finite percent kept", limitWindows({limits: [{label: "5h", percent: 0.5, resetsAt: "", title: ""}]})[0].percent === 0.5);
check("updatedMs prefers Ms field", recordUpdatedMs({updatedAtMs: 123, updatedAt: "2026-01-01T00:00:00.000Z"}) === 123);
check("updatedMs falls back to ISO", recordUpdatedMs({updatedAt: "2026-01-01T00:00:00.000Z"}) === Date.parse("2026-01-01T00:00:00.000Z"));
check("updatedMs unknown is zero", recordUpdatedMs({}) === 0);
check("updatedMs garbage is zero", recordUpdatedMs({updatedAtMs: "fast", updatedAt: "nope"}) === 0);
check("age fresh is empty", recordAgeText(1000000000000, 1000000030000) === "");
check("age minutes", recordAgeText(1000000000000, 1000000300000) === "5m ago");
check("age hours", recordAgeText(1000000000000, 1000007200000) === "2h ago");
check("age days", recordAgeText(1000000000000, 1000259200000) === "3d ago");
check("age unknown is empty", recordAgeText(0, 1000000300000) === "");
check("age future is empty", recordAgeText(1000000005000, 1000000001000) === "");
"""


def extract(source, name):
    # Brace matching that skips line/block comments and quoted strings, so a
    # "{" inside a comment or a help text no longer unbalances the scan.
    # Regex literals with braces are still unsupported; none of the extracted
    # helpers use them.
    start = source.index("function " + name + "(")
    brace = source.index("{", start)
    depth = 0
    i = brace
    n = len(source)
    while i < n:
        ch = source[i]
        if ch == "/" and i + 1 < n and source[i + 1] == "/":
            while i < n and source[i] != "\n":
                i += 1
            continue
        if ch == "/" and i + 1 < n and source[i + 1] == "*":
            i += 2
            while i + 1 < n and not (source[i] == "*" and source[i + 1] == "/"):
                i += 1
            i += 2
            continue
        if ch in ("'", '"', "`"):
            quote = ch
            i += 1
            while i < n and source[i] != quote:
                if source[i] == "\\":
                    i += 1
                i += 1
            i += 1
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return source[start:i + 1]
        i += 1
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
