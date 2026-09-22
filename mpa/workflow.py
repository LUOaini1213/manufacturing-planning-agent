"""Fixed pipeline: assume blanks, solve once, simulate, always replan once. No language and no menu."""

from __future__ import annotations

from mpa.scenario import clone_with_availability, fill_missing_with_shift
from mpa.simulate import simulate
from mpa.solve import solve


def run(scenario: dict) -> dict:
    log = []
    prepared, assumed = fill_missing_with_shift(scenario)
    log.append({"tool": "fill_missing", "assumed": assumed})
    plan = solve(prepared, "cost_min")
    log.append({"tool": "solve", "policy": "cost_min", "ok": plan["ok"], "status": plan.get("status")})
    if not plan["ok"]:
        return _done("fixed_workflow", log, plan, None, "solver refused")
    sim = simulate(prepared, plan, scenario.get("sim_breakdown"))
    log.append({"tool": "simulate", "which": "first_plan", "tardy_units": sim.get("tardy_units"), "on_time_units": sim.get("on_time_units")})
    replanned, sim = _replan(prepared, scenario.get("sim_breakdown"), sim, log)
    return _done("fixed_workflow", log, plan, sim, "replanned once" if replanned else "no breakdown", replanned)


def _replan(scenario: dict, breakdown: dict | None, sim: dict, log: list):
    if not breakdown:
        log.append({"tool": "replan", "skipped": "no breakdown"})
        return None, sim
    machine = next(item for item in scenario["machines"] if item["id"] == breakdown["machine"])
    current = float(machine["available"][breakdown["period"]])
    reduced = max(0.0, current - float(breakdown["hours"]))
    updated = clone_with_availability(scenario, breakdown["machine"], breakdown["period"], reduced)
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
    log.append({"tool": "simulate", "which": "replanned", "on_time_units": checked["on_time_units"], "tardy_units": checked["tardy_units"]})
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
