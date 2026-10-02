"""Replay must consume only authorized machine windows, even after queueing."""
from copy import deepcopy
import math

import pytest

from mpa.simulate import simulate


def scenario(available=(8, 8), *, fab=7, test=1, qty=1, start=None, span=None):
    machine = {"id": "M1", "available": list(available)}
    if start is not None:
        machine["available_start"] = list(start)
    data = {
        "periods": [f"P{i}" for i in range(len(available))],
        "hours_per_period": 8, "overtime_cap_hours": 2,
        "stages": ["fab", "test"], "machines": [machine],
        "orders": [{"id": "A", "qty": qty, "due": 0,
                    "hours": {"fab": {"M1": fab}, "test": {"M1": test}}}],
        "costs": {"overtime_per_hour": 10, "tardiness_per_unit": 5, "unfinished_per_unit": 20},
        "policies": {"cost_min": {"overtime": 10, "tardiness": 5, "unfinished": 20}},
        "sim_seed": 7,
    }
    if span is not None:
        data["period_length_hours"] = span
    return data


def plan(data, *, fab_period=0, test_period=0, overtime=0, fab_units=None, test_units=None):
    order = data["orders"][0]
    rows = []
    for stage, period, units in (("fab", fab_period, fab_units), ("test", test_period, test_units)):
        rows.append({"order": "A", "stage": stage, "machine": "M1", "period": period,
                     "units": order["qty"] if units is None else units,
                     "hours_each": order["hours"][stage]["M1"]})
    return {"ok": True, "assignment": rows,
            "overtime": [{"machine": "M1", "period": 0, "hours": overtime}] if overtime else [],
            "overtime_hours": overtime}


def assert_calendar(data, schedule, result, breakdown=None):
    span = data.get("period_length_hours", data["hours_per_period"] + data["overtime_cap_hours"])
    overtime = {(row["machine"], row["period"]): row["hours"] for row in schedule.get("overtime", [])}
    machines = {row["id"]: row for row in data["machines"]}
    previous = {}
    actual_overtime = 0
    for row in result["execution_segments"]:
        machine = machines[row["machine"]]
        period = row["period"]
        available = machine["available"][period]
        start = period * span + machine.get("available_start", [0] * len(data["periods"]))[period]
        regular_end = start + available
        if breakdown and (row["machine"], period) == (breakdown["machine"], breakdown["period"]):
            loss = min(available, breakdown["hours"])
            start += loss
            available -= loss
        assert available > 0
        assert start - 1e-9 <= row["start"] < row["end"] <= regular_end + overtime.get((row["machine"], period), 0) + 1e-9
        assert row["start"] >= row["planned_period"] * span
        assert row["start"] >= previous.get(row["machine"], 0) - 1e-9
        previous[row["machine"]] = row["end"]
        actual_overtime += max(0, row["end"] - max(row["start"], regular_end))
    assert result["actual_overtime_hours"] == pytest.approx(actual_overtime, abs=1e-4)
    finished_fab = {(row["order"], row["unit"]): row["finish"] for row in result["stage_finishes"] if row["stage"] == "fab"}
    for row in result["execution_segments"]:
        if row["stage"] == "test":
            assert row["start"] >= finished_fab[(row["order"], row["unit"])] - 1e-9


def test_slow_work_cannot_finish_during_shutdown_or_unpaid_overtime():
    data = scenario((8, 0))
    schedule = plan(data)
    result = simulate(data, schedule, pace=1.15)
    assert result["unfinished_units"] == 1
    assert result["on_time_units"] == result["tardy_units"] == 0
    assert result["makespan"] == 8
    assert result["actual_overtime_hours"] == result["scheduled_overtime_hours"] == 0
    assert result["finishes"] == []
    assert_calendar(data, schedule, result)


def test_stage_pauses_through_closure_and_resumes_on_same_machine():
    data = scenario()
    schedule = plan(data)
    result = simulate(data, schedule, pace=1.15)
    assert result["unfinished_units"] == 0
    assert result["tardy_units"] == 1
    assert result["finishes"][0]["finish"] == pytest.approx(11.2)
    assert result["finishes"][0]["due_at"] == 10
    intervals = [(row["start"], row["end"]) for row in result["execution_segments"]]
    assert len(intervals) == 3
    for actual, expected in zip(intervals, [(0, 8), (10, 10.05), (10.05, 11.2)]):
        assert actual == pytest.approx(expected)
    assert_calendar(data, schedule, result)


def test_authorized_overtime_is_inside_its_period_and_due_boundary():
    data = scenario((8,))
    schedule = plan(data, overtime=1.2)
    result = simulate(data, schedule, pace=1.15)
    assert result["on_time_units"] == 1
    assert result["finishes"][0]["finish"] == pytest.approx(9.2)
    assert result["finishes"][0]["due_at"] == 10
    assert result["actual_overtime_hours"] == pytest.approx(1.2)
    assert result["scheduled_overtime_hours"] == 1.2
    assert result["accounting_cost"] == 12
    assert_calendar(data, schedule, result)


def test_reserved_overtime_cost_is_separate_from_actual_consumption():
    data = scenario((8,), fab=1, test=1)
    schedule = plan(data, overtime=2)
    result = simulate(data, schedule, pace=1)
    assert result["scheduled_overtime_hours"] == 2
    assert result["actual_overtime_hours"] == 0
    assert result["accounting_cost"] == 20
    assert_calendar(data, schedule, result)


def test_queue_wait_rechecks_window_and_keeps_paused_stage_ahead_of_later_work():
    data = scenario(qty=2, fab=3.5, test=0.5)
    schedule = plan(data)
    result = simulate(data, schedule, pace=1.15)
    assert result["unfinished_units"] == 0
    assert result["makespan"] == pytest.approx(11.2)
    after_reopen = [row for row in result["execution_segments"] if row["period"] == 1]
    assert after_reopen[0]["stage"] == "fab"
    assert after_reopen[0]["unit"] == 1
    assert_calendar(data, schedule, result)


def test_jobs_cannot_run_before_planned_period_with_explicit_span_and_offset():
    data = scenario((4, 4), fab=1, test=1, start=(2, 3), span=24)
    schedule = plan(data, fab_period=1, test_period=1)
    result = simulate(data, schedule, pace=1)
    assert result["finishes"][0]["finish"] == 29
    assert result["finishes"][0]["due_at"] == 24
    assert result["tardy_units"] == 1
    assert_calendar(data, schedule, result)


@pytest.mark.parametrize("lost", [8, 20])
def test_fully_lost_regular_shift_cannot_be_reopened_by_reserved_overtime(lost):
    data = scenario((8,), fab=1, test=1)
    schedule = plan(data, overtime=2)
    outage = {"machine": "M1", "period": 0, "hours": lost}
    result = simulate(data, schedule, outage, pace=1)
    assert result["unfinished_units"] == 1
    assert result["execution_segments"] == []
    assert result["actual_overtime_hours"] == 0
    assert result["scheduled_overtime_hours"] == 2
    assert result["accounting_cost"] == 40


def test_breakdown_removes_shift_beginning_after_existing_offset():
    data = scenario((6,), fab=1, test=1, start=(2,))
    schedule = plan(data)
    outage = {"machine": "M1", "period": 0, "hours": 3}
    result = simulate(data, schedule, outage, pace=1)
    assert result["execution_segments"][0]["start"] == 5
    assert result["finishes"][0]["finish"] == 7
    assert_calendar(data, schedule, result, outage)


def test_fab_only_work_in_progress_still_consumes_machine_time():
    data = scenario((8,), qty=2, fab=3, test=1)
    schedule = plan(data, test_units=1)
    result = simulate(data, schedule, pace=1)
    assert result["unfinished_units"] == 1
    assert result["on_time_units"] == 1
    assert sum(row["end"] - row["start"] for row in result["execution_segments"]) == 7
    assert sum(row["stage"] == "fab" for row in result["stage_finishes"]) == 2
    assert_calendar(data, schedule, result)


def test_noise_is_fixed_per_order_unit_stage_before_queueing():
    data = scenario((8, 8), qty=2, fab=1, test=1)
    early = plan(data)
    late = plan(data, fab_period=1, test_period=1)
    first = simulate(data, early)
    second = simulate(data, late)
    signature = lambda result: {(row["order"], row["unit"], row["stage"]): row["multiplier"] for row in result["stage_jobs"]}
    assert signature(first) == signature(second)
    reordered = deepcopy(early)
    reordered["assignment"].reverse()
    assert first == simulate(data, reordered)


@pytest.mark.parametrize("pace", [0, -1, math.nan, math.inf, -math.inf, True, "1"])
def test_invalid_pace_is_rejected(pace):
    data = scenario()
    with pytest.raises(ValueError, match="pace"):
        simulate(data, plan(data), pace=pace)


@pytest.mark.parametrize("field,value", [("order", "unknown"), ("stage", "paint"), ("machine", "unknown"),
    ("period", -1), ("period", 2), ("period", True), ("units", 1.5), ("units", -1), ("units", 2),
    ("hours_each", math.nan), ("hours_each", -1), ("hours_each", 0.1)])
def test_invalid_external_assignment_is_rejected_instead_of_dropped(field, value):
    data = scenario()
    schedule = plan(data)
    schedule["assignment"][0][field] = value
    with pytest.raises(ValueError):
        simulate(data, schedule, pace=1)


def test_test_units_without_matching_prior_fabrication_are_rejected():
    data = scenario()
    for schedule in (plan(data, fab_units=0), plan(data, fab_period=1, test_period=0)):
        with pytest.raises(ValueError):
            simulate(data, schedule, pace=1)


@pytest.mark.parametrize("hours", [-1, math.nan, math.inf, 3])
def test_invalid_overtime_authorization_is_rejected(hours):
    data = scenario()
    with pytest.raises(ValueError):
        simulate(data, plan(data, overtime=hours), pace=1)


def test_overtime_total_cannot_disagree_with_machine_authorizations():
    data = scenario()
    schedule = plan(data, overtime=1)
    schedule["overtime_hours"] = 0
    with pytest.raises(ValueError):
        simulate(data, schedule, pace=1)


def test_external_aggregate_allocation_cannot_exceed_authorized_capacity():
    data = scenario((7, 8))
    with pytest.raises(ValueError, match="availability"):
        simulate(data, plan(data), pace=1)


def test_zero_units_do_not_create_work_and_zero_availability_cannot_authorize_overtime():
    data = scenario((0, 8))
    empty = plan(data, fab_units=0, test_units=0)
    result = simulate(data, empty, pace=1)
    assert result["execution_segments"] == []
    assert result["unfinished_units"] == 1
    empty["overtime"] = [{"machine": "M1", "period": 0, "hours": 1}]
    empty["overtime_hours"] = 1
    with pytest.raises(ValueError, match="zero-availability"):
        simulate(data, empty, pace=1)


def test_precedence_delay_on_another_machine_cannot_cross_its_closed_window():
    data = scenario((8, 8), fab=7, test=2)
    data["machines"].append({"id": "M2", "available": [8, 8]})
    data["orders"][0]["hours"]["test"] = {"M2": 2}
    schedule = {"ok": True, "assignment": [
        {"order": "A", "stage": "fab", "machine": "M1", "period": 0, "units": 1, "hours_each": 7},
        {"order": "A", "stage": "test", "machine": "M2", "period": 0, "units": 1, "hours_each": 2},
    ], "overtime": [], "overtime_hours": 0}
    result = simulate(data, schedule, pace=1)
    assert result["finishes"][0]["finish"] == 11
    assert result["tardy_units"] == 1
    assert_calendar(data, schedule, result)


def test_inputs_are_unchanged_and_an_unsolved_plan_keeps_its_false_result():
    data = scenario()
    schedule = plan(data)
    before = deepcopy((data, schedule))
    simulate(data, schedule, pace=1)
    assert (data, schedule) == before
    assert simulate(data, {"ok": False}, pace=1) == {"ok": False, "reason": "no plan to simulate"}


def test_noise_identity_survives_a_change_in_actual_order_queue():
    data = scenario(fab=1, test=1)
    second_order = deepcopy(data["orders"][0])
    second_order.update(id="B", due=1)
    data["orders"].append(second_order)
    early = plan(data)
    early["assignment"].extend([dict(row, order="B") for row in early["assignment"][:]])
    late = deepcopy(early)
    for row in late["assignment"]:
        if row["order"] == "A":
            row["period"] = 1
    first, second = simulate(data, early), simulate(data, late)
    assert first["execution_segments"][0]["order"] == "A"
    assert second["execution_segments"][0]["order"] == "B"
    signature = lambda result: {(row["order"], row["unit"], row["stage"]): row["multiplier"] for row in result["stage_jobs"]}
    assert signature(first) == signature(second)
    assert_calendar(data, early, first)
    assert_calendar(data, late, second)


def test_end_of_authorized_overtime_does_not_overlap_next_period_work():
    data = scenario(fab=4.5, test=0.5, qty=2)
    data["orders"].append({"id": "B", "qty": 1, "due": 1,
                           "hours": {"fab": {"M1": 1}, "test": {"M1": 1}}})
    schedule = plan(data, overtime=2)
    schedule["assignment"].extend([
        {"order": "B", "stage": stage, "machine": "M1", "period": 1, "units": 1, "hours_each": 1}
        for stage in ("fab", "test")])
    result = simulate(data, schedule, pace=1)
    assert result["on_time_units"] == 3
    assert result["actual_overtime_hours"] == 2
    assert max(row["end"] for row in result["execution_segments"] if row["order"] == "A") == 10
    assert min(row["start"] for row in result["execution_segments"] if row["order"] == "B") == 10
    assert_calendar(data, schedule, result)
