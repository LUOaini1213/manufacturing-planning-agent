"""Pin the threshold sweep: sampled rows regenerate exactly, the summary follows the rows,
and the README table and English TL;DR quote the committed summary."""

import json
import re
from pathlib import Path

from mpa.agent import DUE_FIRST_LOSS_FRACTION, run as run_agent
from mpa.evaluate import evaluate
from mpa.scenario import load
from mpa.sweep import HOURS, SEEDS, readme_table, summarize, sweep

ROOT = Path(__file__).resolve().parents[1]
COMMITTED = json.loads((ROOT / "results" / "sweep.json").read_text(encoding="utf-8"))
README = (ROOT / "README.md").read_text(encoding="utf-8")
# Boundary (0 h), first point below it (1 h) and no outage (8 h); seed 7 is the published one,
# seed 5 is where the Agent is more expensive, seed 8 is where forced due_first loses at 8 h.
SAMPLE_HOURS = (0, 1, 8)
SAMPLE_SEEDS = (5, 7, 8)


def _row(hours, seed):
    return next(row for row in COMMITTED["rows"] if row["m2_d2_hours"] == hours and row["seed"] == seed)


def test_committed_sweep_covers_the_full_grid_with_the_current_threshold():
    assert COMMITTED["config"]["m2_d2_hours"] == list(HOURS)
    assert COMMITTED["config"]["seeds"] == list(SEEDS)
    assert COMMITTED["config"]["due_first_loss_fraction"] == DUE_FIRST_LOSS_FRACTION
    assert len(COMMITTED["rows"]) == len(HOURS) * len(SEEDS) == 180


def test_sampled_rows_regenerate_exactly():
    fresh = json.loads(json.dumps(sweep(SAMPLE_HOURS, SAMPLE_SEEDS)))
    assert fresh["rows"] == [_row(h, s) for h in SAMPLE_HOURS for s in SAMPLE_SEEDS]


def test_committed_summary_is_computed_from_committed_rows():
    assert json.loads(json.dumps(summarize(COMMITTED["rows"]))) == COMMITTED["summary"]


def test_published_scenario_is_the_zero_hour_seed_seven_row():
    report = evaluate()
    surge = next(item for item in report["scenarios"] if item["name"] == "surge_downtime")
    row = _row(0, 7)
    for arm in ("rule", "fixed_workflow", "agent"):
        assert row[arm]["replay_cost"] == surge[arm]["sim_accounting_cost"]
        assert row[arm]["on_time"] == surge[arm]["sim_on_time_units"]
    assert row["agent_policy"] == "due_first"
    assert row["fab_hours_lost_fraction"] == DUE_FIRST_LOSS_FRACTION
    # Without the policy switch the Agent replays exactly like the fixed workflow.
    assert row["agent_forced_cost_min"] == row["fixed_workflow"]
    forced = row["agent_forced_cost_min"]
    assert (f"回放为准时 {forced['on_time']}、延期 {forced['tardy']}、未完工 {forced['unfinished']}、"
            f"费用 {forced['replay_cost']:g}，与固定流程相同") in README


def test_policy_flips_just_below_the_boundary():
    scenario = load(ROOT / "scenarios" / "surge_downtime.json")
    next(m for m in scenario["machines"] if m["id"] == "M2")["available"][1] = 0.5
    choice = next(item for item in run_agent(scenario)["log"] if item["tool"] == "select_policy")
    assert choice["fab_hours_lost_fraction"] < DUE_FIRST_LOSS_FRACTION
    assert choice["choice"] == "cost_min"


def test_readme_table_and_bullets_quote_the_committed_summary():
    assert readme_table(COMMITTED["summary"]) in README
    by_hours = {row["m2_d2_hours"]: row for row in COMMITTED["summary"]["by_m2_d2_hours"]}
    zero = by_hours[0]["agent_vs_fixed_workflow"]
    rest = [row for hours, row in by_hours.items() if hours != 0]
    equal_rest = sum(row["agent_vs_fixed_workflow"]["equal"] for row in rest)
    assert all(row["agent_policy"] == {"cost_min": len(SEEDS)} for row in rest)
    assert equal_rest == sum(row["scenarios"] for row in rest)
    forced8 = by_hours[8]["forced_due_first_vs_forced_cost_min"]
    assert all(by_hours[h]["forced_due_first_vs_forced_cost_min"]["worse"] == len(SEEDS) for h in range(1, 8))
    assert f"更便宜 {zero['better']} 次、更贵 {zero['worse']} 次" in README
    assert f"{equal_rest} 个场景与固定流程逐个相同" in README
    assert f"强制 `due_first` 在 20 个种子里有 {forced8['better']} 次更便宜" in README
    total = len(COMMITTED["rows"])
    assert (f"In a {total}-run sweep (M2 D2 availability 0-8 h x seeds 1-20) the Agent is cheaper than the "
            f"fixed workflow in {zero['better']} of {len(SEEDS)} seeds at 0 h (more expensive in {zero['worse']}) "
            f"and identical in all {equal_rest} runs at 1-8 h; at 8 h, forcing `due_first` would have been "
            f"cheaper in {forced8['better']} of {len(SEEDS)} seeds") in README
    assert f"at least {DUE_FIRST_LOSS_FRACTION:.0%} of fab hours" in README


def test_tldr_quotes_the_published_replays_and_the_clsp_numbers():
    report = evaluate()
    surge = next(item for item in report["scenarios"] if item["name"] == "surge_downtime")
    parts = []
    for arm, label in (("rule", "rule"), ("fixed_workflow", "fixed workflow"), ("agent", "Agent")):
        row = surge[arm]
        parts.append(f"{label} {row['sim_on_time_units']}/{row['sim_tardy_units']}/{row['sim_unfinished_units']} "
                     f"at cost {row['sim_accounting_cost']:g}")
    assert "replay on-time/late/unfinished: " + ", ".join(parts) + "." in README
    clsp = json.loads((ROOT / "results" / "trigeiro_f1.json").read_text(encoding="utf-8"))
    best = min(clsp[name]["objective"] for name in ("merge_earlier", "coordinate_plan"))
    assert f"HiGHS optimum {clsp['highs']['objective']:g} vs {best:g} for the best heuristic here" in README
    # First screen: title, summary, four bullets and the pointer line, before the Chinese body.
    first_screen = README.split("The rest of this page is in Chinese.", 1)[0]
    assert len([line for line in first_screen.splitlines() if line.strip()]) <= 7


def test_relax_denial_is_keyword_triggered_and_all_arms_leave_due_dates_alone():
    surge = load(ROOT / "scenarios" / "surge_downtime.json")
    paraphrase = dict(surge, request="把交期都推迟到 D2")
    assert "把交期都推迟到 D2" in README
    assert not any(item["tool"] == "relax_due_dates" for item in run_agent(paraphrase)["log"])
    report = evaluate()
    by_name = {item["name"]: item for item in report["scenarios"]}
    for arm in ("rule", "fixed_workflow", "agent"):
        assert by_name["contradictory_relax"][arm]["plan"]["assignment"] == by_name["surge_downtime"][arm]["plan"]["assignment"]
        for key in ("on_time_units", "tardy_units", "unfinished_units", "accounting_cost", "sim_accounting_cost"):
            assert by_name["contradictory_relax"][arm][key] == by_name["surge_downtime"][arm][key]


def test_readme_ci_sentence_matches_the_workflow():
    ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    sentence = next(line for line in README.splitlines() if line.startswith("CI 在 "))
    for module in re.findall(r"`(mpa\.[a-z]+)`", sentence):
        assert f"python -m {module} " in ci
    assert "python -m mpa.sweep --check" in sentence and "python -m mpa.sweep --check" in ci
    for command in ("node --check", "node --test", "python tests/mutate.py"):
        assert command in ci
    assert "python: ['3.11', '3.12']" in ci and "os: windows-latest" in ci
    assert "Ubuntu（Python 3.11、3.12）和 Windows（Python 3.11）" in sentence
