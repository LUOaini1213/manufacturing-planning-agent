from copy import deepcopy

import pytest

from mpa import agent, workflow
from mpa.scenario import load
from mpa.reporting import ROOT


def replay(*, tardy=0, unfinished=0, overtime=0):
    return {"ok": True, "on_time_units": 16 - tardy - unfinished, "tardy_units": tardy,
            "unfinished_units": unfinished, "scheduled_overtime_hours": overtime, "makespan": 18}


@pytest.mark.parametrize("module,policy", [(agent, "due_first"), (workflow, "cost_min")])
def test_replan_preserves_real_outage_start_without_applying_twice(monkeypatch, module, policy):
    scenario = load(ROOT / "scenarios" / "surge_downtime.json")
    original = deepcopy(scenario)
    observed = []
    plan = {"ok": True, "status": "optimal", "accounting_cost": 0}

    def solve_checked(updated, selected):
        assert selected == policy
        m1 = next(m for m in updated["machines"] if m["id"] == "M1")
        assert m1["available"] == [8, 5]
        assert m1["available_start"] == [0, 3]
        observed.append(updated)
        return plan

    def replay_checked(updated, selected, breakdown):
        assert updated is observed[0]
        assert selected is plan
        assert breakdown is None  # outage is in the calendar now, not omitted
        return replay(tardy=1)

    monkeypatch.setattr(module, "solve", solve_checked)
    monkeypatch.setattr(module, "simulate", replay_checked)
    log = [{"tool": "select_policy", "choice": policy}]
    if module is agent:
        chosen, checked = module._replan_once(scenario, replay(tardy=2), log)
    else:
        chosen, checked = module._replan(scenario, scenario["sim_breakdown"], replay(tardy=2), log)
    assert chosen is plan
    assert checked["tardy_units"] == 1
    assert scenario == original


@pytest.mark.parametrize("candidate,accepted", [
    (replay(tardy=3), False),
    (replay(unfinished=1), False),
    (replay(tardy=1), True),
    (replay(tardy=2), True),
    (replay(overtime=20), False),
])
def test_agent_compares_replayed_policy_cost_before_recommending(monkeypatch, candidate, accepted):
    scenario = load(ROOT / "scenarios" / "surge_downtime.json")
    candidate_plan = {"ok": True, "status": "optimal", "accounting_cost": 0}
    monkeypatch.setattr(agent, "solve", lambda *_: candidate_plan)
    monkeypatch.setattr(agent, "simulate", lambda *_: candidate)
    initial = replay(tardy=2)
    log = [{"tool": "select_policy", "choice": "due_first"}]
    plan, result = agent._replan_once(scenario, initial, log)
    decision = log[-1]
    assert decision["tool"] == "compare_replays"
    assert decision["initial_score"] == 1000
    assert decision["accepted"] is accepted
    assert (plan is candidate_plan) is accepted
    assert result is (candidate if accepted else initial)


def test_actual_agent_retains_initial_and_selected_replay():
    scenario = load(ROOT / "scenarios" / "surge_downtime.json")
    original = deepcopy(scenario)
    result = agent.run(scenario)
    assert result["initial_plan"]["ok"]
    assert result["first_simulation"]["ok"]
    assert result["simulation"]["ok"]
    assert result["model_calls"] == 0
    assert result["needs_human_confirm"]
    assert scenario == original
    attempts = [r for r in result["log"] if r["tool"] == "replan" and "skipped" not in r]
    assert len(attempts) <= 1
    if result["replanned"]:
        policy = result["plan"]["policy"]
        assert agent._replay_score(scenario, result["simulation"], policy) <= agent._replay_score(scenario, result["first_simulation"], policy) + 1e-6


def test_feasible_aggregate_allocation_is_not_an_executable_schedule():
    from mpa.simulate import simulate
    from mpa.solve import solve

    scenario = {
        "periods": ["P1"], "hours_per_period": 2, "overtime_cap_hours": 0,
        "stages": ["fab", "test"],
        "machines": [{"id": "F", "available": [2]}, {"id": "T", "available": [2]}],
        "orders": [{"id": "A", "qty": 2, "due": 0, "hours": {"fab": {"F": 1}, "test": {"T": 1}}}],
        "costs": {"overtime_per_hour": 1, "tardiness_per_unit": 1, "unfinished_per_unit": 100},
        "policies": {"cost_min": {"overtime": 1, "tardiness": 1, "unfinished": 100}},
    }
    plan = solve(scenario, "cost_min")
    assert plan["ok"]
    assert plan["on_time_units"] == 2
    checked = simulate(scenario, plan, pace=1)
    # F makes units during [0,1], [1,2]. T can only finish the first at t=2.
    assert checked["on_time_units"] == 1
    assert checked["unfinished_units"] == 1
    assert checked["tardy_units"] == 0
    assert checked["accounting_cost"] == 100
    assert checked["finishes"][0]["finish"] == 2
    assert all(row["end"] <= 2 for row in checked["execution_segments"])
