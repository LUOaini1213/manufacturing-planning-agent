"""Small, independently countable downtime cases for both planning methods."""

from copy import deepcopy

import pytest

from mpa.rules import earliest_due_date
from mpa.solve import solve


def _scenario(available):
    return {
        "periods": [f"D{i + 1}" for i in range(len(available))],
        "hours_per_period": 8,
        "overtime_cap_hours": 2,
        "stages": ["fab", "test"],
        "costs": {"overtime_per_hour": 80, "tardiness_per_unit": 50, "unfinished_per_unit": 200},
        "policies": {"cost_min": {"overtime": 80, "tardiness": 50, "unfinished": 200}},
        "machines": [{"id": "M1", "available": available}],
        "orders": [{"id": "A", "qty": 1, "due": len(available) - 1,
                    "hours": {"fab": {"M1": 1}, "test": {"M1": 1}}}],
    }


@pytest.mark.parametrize("planner", [solve, earliest_due_date])
@pytest.mark.parametrize("hours_each", [1, 1e-10])
def test_zero_regular_availability_cannot_be_revived_by_overtime(planner, hours_each):
    # Two processing hours would fit the overtime cap, but the machine is down.
    scenario = _scenario([0])
    for rates in scenario["orders"][0]["hours"].values():
        rates["M1"] = hours_each
    before = deepcopy(scenario)
    plan = planner(scenario)
    assert plan["ok"]
    assert plan["assignment"] == []
    assert plan["overtime"] == []
    assert plan["on_time_units"] == 0
    assert plan["unfinished_units"] == 1
    assert plan["overtime_hours"] == 0
    assert plan["accounting_cost"] == 200
    assert scenario == before


@pytest.mark.parametrize("planner", [solve, earliest_due_date])
def test_positive_partial_shift_can_still_use_overtime(planner):
    # One finished item needs 2 h: 0.5 regular + 1.5 overtime.
    plan = planner(_scenario([0.5]))
    assert plan["ok"]
    assert plan["on_time_units"] == 1
    assert plan["unfinished_units"] == 0
    assert plan["overtime_hours"] == pytest.approx(1.5)
    assert plan["accounting_cost"] == 120


@pytest.mark.parametrize("planner", [solve, earliest_due_date])
def test_down_machine_can_recover_in_a_later_period(planner):
    plan = planner(_scenario([0, 2]))
    assert plan["ok"]
    assert {row["period"] for row in plan["assignment"]} == {1}
    assert plan["on_time_units"] == 1
    assert plan["unfinished_units"] == 0
    assert plan["overtime_hours"] == 0


@pytest.mark.parametrize("planner", [solve, earliest_due_date])
def test_missing_availability_still_refuses_instead_of_assuming_zero(planner):
    plan = planner(_scenario([None]))
    assert not plan["ok"]
    assert plan["status"] == "refused"
    assert "missing availability" in plan["reason"]


@pytest.mark.parametrize("planner", [solve, earliest_due_date])
def test_available_alternative_machine_is_used_instead_of_stopped_machine(planner):
    scenario = _scenario([0])
    scenario["machines"].append({"id": "M2", "available": [2]})
    for rates in scenario["orders"][0]["hours"].values():
        rates["M2"] = 1
    plan = planner(scenario)
    assert plan["on_time_units"] == 1
    assert {row["machine"] for row in plan["assignment"]} == {"M2"}
    assert plan["overtime_hours"] == 0


def test_zero_penalty_weights_do_not_turn_unused_lateness_slack_into_late_units():
    scenario = _scenario([2, 2])
    scenario["orders"][0]["due"] = 0
    scenario["policies"]["cost_min"] = {"overtime": 0, "tardiness": 0, "unfinished": 0}
    plan = solve(scenario)
    completed = sum(row["units"] for row in plan["assignment"] if row["stage"] == "test")
    late = sum(row["units"] for row in plan["assignment"] if row["stage"] == "test" and row["period"] > 0)
    assert plan["tardy_units"] == late
    assert plan["unfinished_units"] == 1 - completed
    assert plan["on_time_units"] == completed - late
    assert plan["orders"][0]["tardy"] == late
    assert plan["accounting_cost"] == pytest.approx(
        50 * late + 200 * (1 - completed) + 80 * plan["overtime_hours"], abs=1e-4
    )


@pytest.mark.parametrize("planner", [solve, earliest_due_date])
def test_small_required_overtime_is_not_lost_to_display_rounding(planner):
    plan = planner(_scenario([1.99996]))
    assert plan["on_time_units"] == 1
    assert len(plan["overtime"]) == 1
    assert plan["overtime"][0]["hours"] == pytest.approx(0.00004, abs=1e-10)
    assert plan["overtime_hours"] == pytest.approx(0.00004, abs=1e-10)


@pytest.mark.parametrize("planner", [solve, earliest_due_date])
def test_public_planner_rejects_invalid_input_even_when_not_loaded_from_json(planner):
    scenario = _scenario([True])
    with pytest.raises(ValueError):
        planner(scenario)
