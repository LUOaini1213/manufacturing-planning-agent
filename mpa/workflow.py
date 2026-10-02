"""Fixed pipeline: assume blanks, solve once, simulate, always replan once. No language and no menu."""

from __future__ import annotations

from mpa.scenario import apply_breakdown, fill_missing_with_shift, normalize
from mpa.simulate import simulate
from mpa.solve import solve


def run(scenario: dict) -> dict:
    scenario = normalize(scenario)
    log = []
    prepared, assumed = fill_missing_with_shift(scenario)
    log.append({"tool": "fill_missing", "assumed": assumed})
    plan = solve(prepared, "cost_min")
    log.append({"tool": "solve", "policy": "cost_min", "ok": plan["ok"], "status": plan.get("status")})
    if not plan["ok"]:
        return _done("fixed_workflow", log, plan, None, "solver refused")
    sim = simulate(prepared, plan, scenario.get("sim_breakdown"))
    log.append({"tool": "simulate", "which": "first_plan", "tardy_units": sim.get("tardy_units"), "on_time_units": sim.get("on_time_units"), "unfinished_units": sim.get("unfinished_units")})
    first_sim = sim
    replanned, sim = _replan(prepared, scenario.get("sim_breakdown"), sim, log)
    result = _done("fixed_workflow", log, plan, sim, "offline re-solve once" if replanned else "no replacement", replanned)
    result["initial_plan"] = plan
    result["first_simulation"] = first_sim
    return result


def _replan(scenario: dict, breakdown: dict | None, sim: dict, log: list):
    if not breakdown:
        log.append({"tool": "replan", "skipped": "no breakdown"})
        return None, sim
    machine = next(item for item in scenario["machines"] if item["id"] == breakdown["machine"])
    current = float(machine["available"][breakdown["period"]])
    updated = apply_breakdown(scenario, breakdown)
    reduced = next(item for item in updated["machines"] if item["id"] == breakdown["machine"])["available"][breakdown["period"]]
    plan = solve(updated, "cost_min")
    log.append(
        {
            "tool": "replan",
            "change": f"{breakdown['machine']} period {breakdown['period']} availability {current:g} -> {reduced:g}",
            "ok": plan["ok"],
            "status": plan.get("status"),
            "accounting_cost": plan.get("accounting_cost"),
        }
    )
    if not plan["ok"]:
        return None, sim
    checked = simulate(updated, plan, None)
    log.append({"tool": "simulate", "which": "replanned", "on_time_units": checked["on_time_units"], "tardy_units": checked["tardy_units"], "unfinished_units": checked["unfinished_units"]})
    return plan, checked


def _done(name, log, plan, sim, note, replanned=None) -> dict:
    chosen = replanned or plan
    return {
        "method": name,
        "note": note,
        "plan": chosen,
        "simulation": sim,
        "log": log,
        "model_calls": 0,
        "needs_human_confirm": False,
    }
