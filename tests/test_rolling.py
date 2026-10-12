"""Independent small execution examples; real HiGHS and SimPy, no language model."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import subprocess
import sys
from xml.etree import ElementTree

import pytest

from mpa.rolling import advance_confirmed, gantt, propose
from mpa.rolling_solver import solve_remaining
from mpa.rolling_state import (audit_placements, initial_snapshot, key, metrics, resume,
                               validate_snapshot)
from mpa.scenario import load

ROOT = Path(__file__).resolve().parents[1]


def example():
    data = load(ROOT / "scenarios" / "rolling_outage.json")
    config = data["rolling"]
    return data, initial_snapshot(data, config["initialPlan"], config["currentTime"]), config["event"]


def by_key(snapshot):
    return {key(row): row for row in snapshot["operations"]}


def check_independently(data, state, prefix):
    assert state["executedPrefix"][:len(prefix)] == prefix
    orders = {order["id"]: order for order in data["orders"]}
    operations = by_key(state)
    durations, machine_intervals = {}, {}
    span = data.get("period_length_hours", data["hours_per_period"] + data["overtime_cap_hours"])
    machines = {machine["id"]: machine for machine in data["machines"]}
    overtime = {(row["machine"], row["period"]): row["hours"] for row in state["overtime"]}
    for row in state["executedPrefix"]:
        assert row["start"] < row["end"] <= state["currentTime"]
        assert row["machine"] in orders[row["order"]]["hours"][row["stage"]]
        machine, period = machines[row["machine"]], row["period"]
        opening = period * span + machine.get("available_start", [0] * len(data["periods"]))[period]
        regular_end = opening + machine["available"][period]
        assert machine["available"][period] > 0
        assert opening <= row["start"] < row["end"] <= regular_end + overtime.get((row["machine"], period), 0) + 1e-8
        assert row["overtimeHours"] == pytest.approx(max(0, row["end"] - max(row["start"], regular_end)))
        for outage in state["events"]:
            if outage["machine"] == row["machine"]:
                assert row["end"] <= outage["start"] or row["start"] >= outage["end"]
        durations[key(row)] = durations.get(key(row), 0) + row["end"] - row["start"]
        machine_intervals.setdefault(row["machine"], []).append((row["start"], row["end"]))
        if row["stage"] == "test":
            assert row["start"] >= operations[(row["order"], row["unit"], "fab")]["completedAt"]
    for spans in machine_intervals.values():
        spans.sort()
        assert all(previous[1] <= later[0] + 1e-8 for previous, later in zip(spans, spans[1:]))
    for identity, operation in operations.items():
        assert durations.get(identity, 0) == pytest.approx(operation["processedHours"])
        if operation["machine"]:
            expected = orders[identity[0]]["hours"][identity[2]][operation["machine"]]
            assert operation["processedHours"] + operation["remainingHours"] == pytest.approx(expected)
        if operation["state"] == "completed":
            assert operation["remainingHours"] == pytest.approx(0)


def test_real_simpy_cut_has_completed_unit_and_partially_processed_wip():
    data, snapshot, _ = example()
    operations = by_key(snapshot)
    assert snapshot["currentTime"] == 2
    assert operations[("A", 0, "test")]["state"] == "completed"
    assert operations[("A", 0, "test")]["completedAt"] == 2
    wip = operations[("A", 1, "fab")]
    assert (wip["state"], wip["processedHours"], wip["remainingHours"], wip["startedAt"]) == ("wip", 0.5, 1, 1.5)
    assert len(snapshot["operations"]) == 8  # all demand, not only completed records
    assert snapshot["executedPrefix"][-1]["start"] == 1.5
    assert snapshot["executedPrefix"][-1]["end"] == 2
    check_independently(data, snapshot, snapshot["executedPrefix"])


@pytest.mark.parametrize("cutoff", [0, 1.5, 2.25, 3, 8, 10])
def test_cut_boundary_and_fractional_progress_are_real_execution(cutoff):
    data = load(ROOT / "scenarios" / "rolling_outage.json")
    state = initial_snapshot(data, data["rolling"]["initialPlan"], cutoff)
    assert state["currentTime"] == cutoff
    check_independently(data, state, [])


def test_real_solver_changes_only_unstarted_routes_and_resume_does_not_repeat_work():
    data, snapshot, event = example()
    frozen = deepcopy(snapshot)
    report = propose(data, snapshot, event)
    assert snapshot == frozen
    assert report["solver"]["status"] == "optimal_on_conservative_grid"
    assert report["solver"]["mipGap"] == 0
    assert report["model_calls"] == 0 and report["needs_human_confirm"] is True
    assert report["baseline"]["metrics"]["unfinishedUnits"] == 2
    assert report["candidate"]["metrics"]["onTimeUnits"] == 4
    assert report["candidate"]["metrics"]["unfinishedUnits"] == 0
    candidate = report["candidate"]["state"]
    assert by_key(candidate)[("A", 0, "test")] == by_key(snapshot)[("A", 0, "test")]
    assert by_key(candidate)[("A", 1, "fab")]["machine"] == "M1"
    assert by_key(candidate)[("A", 1, "fab")]["processedHours"] == 1.5
    assert {p["machine"] for p in report["solver"]["placements"] if p["order"] == "B" and p["stage"] == "fab"} == {"M2"}
    check_independently(data, candidate, snapshot["executedPrefix"])
    check_independently(data, report["baseline"]["state"], snapshot["executedPrefix"])


def test_started_work_cannot_switch_to_a_faster_eligible_machine():
    data = load(ROOT / "scenarios" / "rolling_outage.json")
    data["orders"][0]["hours"]["fab"]["M2"] = 0.1
    snapshot = initial_snapshot(data, data["rolling"]["initialPlan"], 2)
    result = propose(data, snapshot, data["rolling"]["event"])
    placement = next(p for p in result["solver"]["placements"] if key(p) == ("A", 1, "fab"))
    assert placement["machine"] == "M1" and placement["holdStart"] == 2
    assert sum(s["end"] - s["start"] for s in placement["segments"]) == pytest.approx(1)


def crossing_closure():
    data = {"periods": ["P0", "P1"], "hours_per_period": 8, "overtime_cap_hours": 2,
            "stages": ["fab", "test"], "machines": [{"id": "M1", "available": [8, 8]}],
            "orders": [{"id": "A", "qty": 1, "due": 0, "hours": {"fab": {"M1": 9}, "test": {"M1": 1}}}],
            "costs": {"overtime_per_hour": 10, "tardiness_per_unit": 5, "unfinished_per_unit": 20},
            "policies": {"cost_min": {"overtime": 10, "tardiness": 5, "unfinished": 20}}}
    plan = {"ok": True, "assignment": [
        {"order": "A", "stage": "fab", "machine": "M1", "period": 0, "units": 1, "hours_each": 9},
        {"order": "A", "stage": "test", "machine": "M1", "period": 1, "units": 1, "hours_each": 1}],
        "overtime": [{"machine": "M1", "period": 0, "hours": 1}], "overtime_hours": 1}
    return data, initial_snapshot(data, plan, 4)


def test_wip_pauses_through_outage_closure_and_resumes_exact_remaining_work():
    data, snapshot = crossing_closure()
    report = propose(data, snapshot, {"machine": "M1", "start": 4, "end": 8})
    placement = next(p for p in report["solver"]["placements"] if p["stage"] == "fab")
    assert [(s["start"], s["end"]) for s in placement["segments"]] == [(8, 9), (10, 14)]
    assert report["candidate"]["metrics"] == {"onTimeUnits": 0, "tardyUnits": 1, "unfinishedUnits": 0,
                                            "actualOvertimeHours": 1, "reservedOvertimeHours": 1}
    assert by_key(report["candidate"]["state"])[("A", 0, "fab")]["processedHours"] == 9
    check_independently(data, report["candidate"]["state"], snapshot["executedPrefix"])


def test_wip_remains_explicitly_unfinished_if_no_future_capacity_exists():
    data, snapshot = crossing_closure()
    report = propose(data, snapshot, {"machine": "M1", "start": 4, "end": 30})
    remaining = by_key(report["candidate"]["state"])[("A", 0, "fab")]
    assert remaining["state"] == "wip" and remaining["processedHours"] == 4 and remaining["remainingHours"] == 5
    assert report["candidate"]["metrics"]["unfinishedUnits"] == 1
    assert by_key(report["candidate"]["state"])[("A", 0, "test")]["state"] == "unstarted"


def test_two_event_flow_requires_confirmation_and_preserves_both_execution_prefixes():
    data, snapshot, event = example()
    report = propose(data, snapshot, event)
    with pytest.raises(ValueError, match="human confirmation"):
        advance_confirmed(data, snapshot, report, 3.5)
    advanced = advance_confirmed(data, snapshot, report, 3.5, confirmed=True)
    assert advanced["currentTime"] == 3.5
    assert advanced["executedPrefix"][:len(snapshot["executedPrefix"])] == snapshot["executedPrefix"]
    second = propose(data, advanced, {"machine": "M2", "start": 3.5, "end": 5})
    final = advance_confirmed(data, advanced, second, 10, confirmed=True)
    check_independently(data, final, advanced["executedPrefix"])
    assert by_key(final)[("A", 0, "test")] == by_key(snapshot)[("A", 0, "test")]
    with pytest.raises(ValueError, match="stale proposal"):
        advance_confirmed(data, advanced, report, 10, confirmed=True)


@pytest.mark.parametrize("corruption", ["due", "duplicate", "drop", "progress", "fake_finish", "overtime"])
def test_corrupt_or_retargeted_snapshots_cannot_rewrite_completion(corruption):
    data, snapshot, _ = example()
    if corruption == "due":
        # A valid within-horizon relaxation, not an already-invalid period index.
        data["periods"].append("D2")
        for machine in data["machines"]: machine["available"].append(8); machine["available_start"].append(0)
        snapshot = initial_snapshot(data, data["rolling"]["initialPlan"], 2)
        data["orders"][0]["due"] = 1
    elif corruption == "duplicate": snapshot["operations"].append(deepcopy(snapshot["operations"][0]))
    elif corruption == "drop": snapshot["operations"].pop()
    elif corruption == "progress": snapshot["operations"][2]["processedHours"] = 0
    elif corruption == "fake_finish": snapshot["operations"][1]["completedAt"] = 1.6
    elif corruption == "overtime": snapshot["executedPrefix"][0]["overtimeHours"] = 3
    with pytest.raises(ValueError): validate_snapshot(data, snapshot)


def test_event_cannot_change_the_past_or_delete_known_outage_history():
    data, snapshot, _ = example()
    with pytest.raises(ValueError, match="future"):
        propose(data, snapshot, {"machine": "M1", "start": 1, "end": 6})
    with pytest.raises(ValueError, match="only machine"):
        propose(data, snapshot, {"machine": "M1", "start": 2, "end": 6, "due": 1})
    advanced = resume(data, snapshot, [{"machine": "M1", "start": 2, "end": 6}], until=3)
    with pytest.raises(ValueError, match="immutable"):
        resume(data, advanced, [], until=4)


def test_placement_audit_rejects_completed_repetition_and_dropped_or_delayed_wip():
    data, snapshot, event = example()
    report = propose(data, snapshot, event)
    placements = report["solver"]["placements"]
    with pytest.raises(ValueError, match="dropped"):
        audit_placements(data, snapshot, [event], [p for p in placements if key(p) != ("A", 1, "fab")])
    bad = deepcopy(placements); next(p for p in bad if key(p) == ("A", 1, "fab"))["holdStart"] = 6
    with pytest.raises(ValueError, match="retain ownership"):
        audit_placements(data, snapshot, [event], bad)
    duplicate = {**placements[0], "order": "A", "unit": 0, "stage": "fab"}
    with pytest.raises(ValueError, match="completed"):
        audit_placements(data, snapshot, [event], placements + [duplicate])


def test_conservative_grid_and_size_limits_are_declared_not_continuous_optimality():
    data, snapshot, event = example()
    result = solve_remaining(data, snapshot, [event], step=0.5)
    assert "not continuous-time" in result["scope"]
    assert solve_remaining(data, snapshot, [event], step=0.001)["status"] == "size_limit"


def test_worse_grid_candidate_retains_a_feasible_original_suffix():
    # The real original route starts at 2.25 and completes by 2.55. The 0.5h
    # start grid has no complete fabrication slot in that short legal window.
    data = {"periods": ["P0"], "hours_per_period": 8, "overtime_cap_hours": 2,
            "stages": ["fab", "test"],
            "machines": [{"id": "M1", "available": [0.3], "available_start": [2.25]},
                         {"id": "M2", "available": [8]}],
            "orders": [{"id": "A", "qty": 1, "due": 0,
                        "hours": {"fab": {"M1": 0.2}, "test": {"M1": 0.1}}}],
            "costs": {"overtime_per_hour": 10, "tardiness_per_unit": 5, "unfinished_per_unit": 20},
            "policies": {"cost_min": {"overtime": 10, "tardiness": 5, "unfinished": 20}}}
    plan = {"assignment": [{"order": "A", "stage": stage, "machine": "M1", "period": 0,
                            "units": 1, "hours_each": duration} for stage, duration in (("fab", 0.2), ("test", 0.1))],
            "overtime": []}
    snapshot = initial_snapshot(data, plan, 2)
    report = propose(data, snapshot, {"machine": "M2", "start": 2, "end": 2.1})
    assert report["baseline"]["metrics"]["unfinishedUnits"] == 0
    assert report["candidate"]["metrics"]["unfinishedUnits"] == 1
    assert report["baseline"]["policyScore"] == 0 and report["candidate"]["policyScore"] == 20
    assert report["recommendation"] == "keep_original" and "worse" in report["reason"]
    advanced = advance_confirmed(data, snapshot, report, 10, choice="original", confirmed=True)
    assert metrics(data, advanced)["onTimeUnits"] == 1
    check_independently(data, advanced, [])


def test_impossible_start_commitment_cannot_cut_ahead_of_an_occupied_machine():
    data, snapshot, _ = example()
    by_key(snapshot)[("A", 1, "fab")]["startedAt"] = 0.5
    next(c for c in snapshot["startCommitments"] if key(c) == ("A", 1, "fab"))["time"] = 0.5
    with pytest.raises(ValueError, match="exclusive machine ownership"):
        validate_snapshot(data, snapshot)


def test_solver_failure_withholds_replacement_without_rewinding_execution(monkeypatch):
    from types import SimpleNamespace
    import mpa.rolling_solver as solver
    monkeypatch.setattr(solver, "milp", lambda *args, **kwargs: SimpleNamespace(success=False, x=None, message="time limit"))
    data, snapshot, event = example()
    report = propose(data, snapshot, event)
    assert report["candidate"] is None and report["recommendation"] == "keep_original"
    assert report["baseline"]["state"]["executedPrefix"][:len(snapshot["executedPrefix"])] == snapshot["executedPrefix"]
    assert report["needs_human_confirm"] is True


def test_report_svg_and_cli_preserve_old_metrics_and_record_actual_source(tmp_path):
    protected = [ROOT / "results" / "comparison.json", ROOT / "results" / "trigeiro_f1.json"]
    before = [p.read_bytes() for p in protected]
    subprocess.run([sys.executable, "-m", "mpa.rolling", "--output-dir", str(tmp_path)], cwd=ROOT,
                   capture_output=True, check=True, timeout=60)
    report = json.loads((tmp_path / "comparison.json").read_text(encoding="utf-8"))
    assert report["provenance"]["source_commit"]
    assert "mpa/rolling_solver.py" in report["provenance"]["sha256"]
    assert report["provenance"]["sha256"]["mpa/rolling_state.py"] == hashlib.sha256((ROOT / "mpa" / "rolling_state.py").read_text(encoding="utf-8").encode()).hexdigest()
    ElementTree.fromstring((tmp_path / "gantt.svg").read_text(encoding="utf-8"))
    assert "not yet confirmed" in gantt(load(ROOT / "scenarios" / "rolling_outage.json"), report)
    page = (tmp_path / "index.html").read_text(encoding="utf-8")
    assert "Pending human confirmation" in page
    assert "Execution snapshot at hour 2" in page
    assert "2 completed stages, 1 WIP stage, 5 unstarted stages" in page
    assert "<td>A/1 fab</td><td>M1</td><td>0.5</td><td>1</td>" in page
    assert report["provenance"]["source_commit"] in page
    assert [p.read_bytes() for p in protected] == before
