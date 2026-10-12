"""Bounded tool loop. It can refuse, pick a named policy, and replan. It cannot edit due dates."""

from __future__ import annotations

from mpa.scenario import apply_breakdown, missing_availability, normalize
from mpa.simulate import simulate
from mpa.solve import solve

RELAX_MARKERS = ("改到最后", "放宽交期", "改交期", "准时率变成 100", "ignore the due")


def run(scenario: dict) -> dict:
    scenario = normalize(scenario)
    log: list[dict] = []
    request = scenario.get("request") or ""
    if any(marker in request for marker in RELAX_MARKERS):
        log.append(
            {
                "tool": "relax_due_dates",
                "status": "denied",
                "reason": "due dates stay in the data; lateness is a cost, not a constraint to delete",
            }
        )

    missing = missing_availability(scenario)
    log.append({"tool": "audit", "missing": missing})
    if missing:
        log.append({"tool": "solve", "status": "not_called", "reason": "availability is blank"})
        return {
            "method": "agent",
            "note": "abstain",
            "plan": {"ok": False, "status": "abstain", "reason": "missing availability: " + ", ".join(missing)},
            "simulation": None,
            "log": log,
            "model_calls": 0,
            "needs_human_confirm": True,
        }

    policy = _select_policy(scenario, log)
    plan = solve(scenario, policy)
    log.append(
        {
            "tool": "solve",
            "policy": policy,
            "ok": plan["ok"],
            "status": plan.get("status"),
            "accounting_cost": plan.get("accounting_cost"),
            "tardy_units": plan.get("tardy_units"),
        }
    )
    if not plan["ok"]:
        return _result("solver refused", plan, None, log)

    try:
        sim = simulate(scenario, plan, scenario.get("sim_breakdown"))
    except TimeoutError as exc:
        log.append({"tool": "simulate", "status": "timeout", "reason": str(exc)})
        log.append({"tool": "recommend", "status": "withheld", "reason": "simulation did not finish; no replacement plan was invented"})
        return _result("sim timeout", plan, None, log)
    log.append(
        {
            "tool": "simulate",
            "tardy_units": sim["tardy_units"],
            "on_time_units": sim["on_time_units"],
            "makespan": sim["makespan"],
            "unfinished_units": sim["unfinished_units"],
        }
    )

    sim_worse = sim["on_time_units"] < plan["on_time_units"] or sim["unfinished_units"] > plan["unfinished_units"]
    first_sim = sim
    if sim_worse and scenario.get("sim_breakdown"):
        replanned, sim = _replan_once(scenario, sim, log)
    else:
        replanned = None
        log.append({"tool": "replan", "skipped": "simulation did not miss more than the plan"})
    chosen = replanned or plan
    log.append(
        {
            "tool": "recommend",
            "status": "pending_human_confirm",
            "policy": chosen.get("policy"),
            "accounting_cost": chosen.get("accounting_cost"),
            "denied_relax": any(item.get("tool") == "relax_due_dates" for item in log),
        }
    )
    result = _result("pending confirmation", chosen, sim, log, replanned is not None)
    result["initial_plan"] = plan
    result["first_simulation"] = first_sim
    return result


def _select_policy(scenario: dict, log: list[dict]) -> str:
    fab_ids = {machine_id for order in scenario["orders"] for machine_id in order["hours"].get("fab", {})}
    scheduled = 0.0
    available = 0.0
    for machine in scenario["machines"]:
        if machine["id"] not in fab_ids:
            continue
        for hours in machine["available"]:
            scheduled += float(scenario["hours_per_period"])
            available += float(hours)
    lost = max(0.0, scheduled - available)
    fraction = lost / scheduled if scheduled else 0.0
    due_after_first = any(order["due"] > 0 for order in scenario["orders"])
    policy = "due_first" if fraction >= 0.25 and due_after_first else "cost_min"
    log.append(
        {
            "tool": "select_policy",
            "choice": policy,
            "fab_hours_lost_fraction": round(fraction, 4),
            "reason": "named menu only; weights are not free inputs",
        }
    )
    return policy


def _replan_once(scenario: dict, sim: dict, log: list[dict]):
    breakdown = scenario["sim_breakdown"]
    machine = next(item for item in scenario["machines"] if item["id"] == breakdown["machine"])
    current = float(machine["available"][breakdown["period"]])
    updated = apply_breakdown(scenario, breakdown)
    reduced = next(item for item in updated["machines"] if item["id"] == breakdown["machine"])["available"][breakdown["period"]]
    policy = next(item["choice"] for item in log if item["tool"] == "select_policy")
    plan = solve(updated, policy)
    log.append(
        {
            "tool": "replan",
            "change": f"{breakdown['machine']} period {breakdown['period']} {current:g}h -> {reduced:g}h",
            "ok": plan["ok"],
            "status": plan.get("status"),
            "accounting_cost": plan.get("accounting_cost"),
        }
    )
    if not plan["ok"]:
        return None, sim
    checked = simulate(updated, plan, None)
    log.append({"tool": "simulate", "which": "replanned", "on_time_units": checked["on_time_units"], "tardy_units": checked["tardy_units"], "unfinished_units": checked["unfinished_units"]})
    old_score = _replay_score(scenario, sim, policy)
    new_score = _replay_score(scenario, checked, policy)
    accepted = new_score <= old_score + 1e-6
    log.append({"tool": "compare_replays", "policy": policy, "initial_score": old_score,
                "candidate_score": new_score, "accepted": accepted,
                "scope": "offline full-horizon alternatives, not live dispatch"})
    if not accepted:
        return None, sim
    return plan, checked


def _replay_score(scenario: dict, replay: dict, policy: str) -> float:
    weights = scenario["policies"][policy]
    return (replay["tardy_units"] * weights["tardiness"]
            + replay["unfinished_units"] * weights["unfinished"]
            + replay["scheduled_overtime_hours"] * weights["overtime"])


def _result(note: str, plan: dict, sim: dict | None, log: list[dict], replanned: bool = False) -> dict:
    return {
        "method": "agent",
        "note": note,
        "replanned": replanned,
        "plan": plan,
        "simulation": sim,
        "log": log,
        "model_calls": 0,
        "needs_human_confirm": True,
    }
