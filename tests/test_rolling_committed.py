"""The committed rolling report must still be what the current code produces (provenance aside)."""
import json
from pathlib import Path

from mpa.rolling import gantt, propose
from mpa.rolling_state import initial_snapshot
from mpa.scenario import load

ROOT = Path(__file__).resolve().parents[1]
COMMITTED = ROOT / "results" / "rolling"


def _fresh():
    scenario = load(ROOT / "scenarios" / "rolling_outage.json")
    config = scenario["rolling"]
    snapshot = initial_snapshot(scenario, config["initialPlan"], config["currentTime"])
    report = propose(scenario, snapshot, config["event"], step=config["gridStep"])
    return scenario, json.loads(json.dumps(report))


def test_committed_rolling_report_matches_a_fresh_run_except_provenance():
    scenario, fresh = _fresh()
    committed = json.loads((COMMITTED / "comparison.json").read_text(encoding="utf-8"))
    provenance = committed.pop("provenance")
    assert set(fresh) == set(committed)
    for name in sorted(committed):
        assert fresh[name] == committed[name], name
    # The committed input hash is still the hash of the committed scenario.
    assert provenance["input"]["name"] == "rolling_outage.json"
    svg = (COMMITTED / "gantt.svg").read_text(encoding="utf-8").replace("\r\n", "\n")
    assert gantt(scenario, fresh) == svg


def test_readme_rolling_numbers_come_from_the_committed_report():
    committed = json.loads((COMMITTED / "comparison.json").read_text(encoding="utf-8"))
    base, cand = committed["baseline"]["metrics"], committed["candidate"]["metrics"]
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    done = lambda m: m["onTimeUnits"] + m["tardyUnits"]
    assert f"原剩余路线完成 {done(base)}、未完成 {base['unfinishedUnits']}" in readme
    assert f"完成 {done(cand)}、未完成 {cand['unfinishedUnits']}，加班均为 0" in readme
    assert base["reservedOvertimeHours"] == cand["reservedOvertimeHours"] == 0
    assert base["actualOvertimeHours"] == cand["actualOvertimeHours"] == 0
    # README links point at files GitHub renders (SVG, JSON), not raw HTML source.
    assert "](results/rolling/index.html)" not in readme
    assert "](results/rolling/gantt.svg)" in readme
