"""Replay aggregate allocations through legal machine windows, with an auditable trace.

Stages use FIFO resources and retain their place while paused through closures. A
period allocation is a release time, not a guarantee that the within-period fab/test
sequence is executable. Accounting charges reserved overtime; actual usage is separate.
"""
from __future__ import annotations

import math
import random

import simpy

from mpa.scenario import apply_breakdown, normalize, period_length
from mpa.solve import _accounting_cost


def simulate(scenario: dict, plan: dict, breakdown: dict | None = None, pace: float | None = None) -> dict:
    if not isinstance(plan, dict):
        raise ValueError("plan must be an object")
    if not plan.get("ok"):
        return {"ok": False, "reason": "no plan to simulate"}
    scenario = normalize(scenario)
    if any(value is None for machine in scenario["machines"] for value in machine["available"]):
        raise ValueError("cannot simulate missing availability")
    if pace is not None:
        pace = _number(pace, "pace", positive=True)
    overtime = _validate_plan(scenario, plan)
    calendar = apply_breakdown(scenario, breakdown)
    span = period_length(scenario)
    windows = _windows(calendar, overtime, span)
    orders = {order["id"]: order for order in scenario["orders"]}

    # Sample demanded identities before queueing: allocation order and partial plans
    # cannot change another unit's processing-time draw.
    rng = random.Random(int(scenario.get("sim_seed", 0)))
    multipliers = {(order["id"], unit, stage): pace if pace is not None else rng.uniform(0.9, 1.15)
                   for order in sorted(orders.values(), key=lambda row: row["id"])
                   for unit in range(order["qty"]) for stage in scenario["stages"]}
    jobs = _pair_jobs(plan["assignment"])
    stage_jobs = []
    for fab, test in jobs:
        for job in (fab, test):
            if job is None:
                continue
            job["multiplier"] = multipliers[(job["order"], job["unit"], job["stage"])]
            job["required_hours"] = job["hours_each"] * job["multiplier"]
            if not math.isfinite(job["required_hours"]) or job["required_hours"] <= 0:
                raise ValueError("pace produces non-finite or nonpositive processing hours")
            job["remaining_hours"] = job["required_hours"]
            job["finish"] = None
            stage_jobs.append(job)

    env = simpy.Environment()
    resources = {machine["id"]: simpy.Resource(env, capacity=1) for machine in scenario["machines"]}
    segments, stage_finishes, finishes = [], [], []

    def run_stage(job):
        release = job["period"] * span
        if env.now < release:
            yield env.timeout(release - env.now)
        with resources[job["machine"]].request() as request:
            yield request
            # Recheck after queueing; retain the resource across closures so later
            # jobs cannot overtake a partially processed non-preemptive stage.
            for period in range(job["period"], len(scenario["periods"])):
                window = windows[(job["machine"], period)]
                if window["end"] <= max(env.now, window["start"]):
                    continue
                if env.now < window["start"]:
                    yield env.timeout(window["start"] - env.now)
                start = env.now
                remaining = job["remaining_hours"]
                duration = min(remaining, window["end"] - start)
                yield env.timeout(duration)
                segments.append({"order": job["order"], "unit": job["unit"], "stage": job["stage"],
                                 "machine": job["machine"], "planned_period": job["period"],
                                 "period": period, "start": start, "end": env.now,
                                 "overtime_hours": max(0.0, env.now - max(start, window["regular_end"]))})
                job["remaining_hours"] = max(0.0, remaining - duration)
                if math.isclose(duration, remaining, rel_tol=1e-12, abs_tol=1e-12):
                    job["remaining_hours"] = 0.0
                    job["finish"] = env.now
                    stage_finishes.append({"order": job["order"], "unit": job["unit"],
                                           "stage": job["stage"], "machine": job["machine"], "finish": env.now})
                    return env.now
            return None

    def run_unit(fab, test):
        fab_finish = yield from run_stage(fab)
        if fab_finish is None or test is None:
            return
        test_finish = yield from run_stage(test)
        if test_finish is None:
            return
        due_at = (orders[fab["order"]]["due"] + 1) * span
        finishes.append({"order": fab["order"], "unit": fab["unit"], "finish": test_finish,
                         "due_at": due_at, "on_time": test_finish <= due_at + 1e-6})

    for fab, test in sorted(jobs, key=lambda pair: (pair[0]["period"], pair[0]["order"], pair[0]["unit"])):
        env.process(run_unit(fab, test))
    env.run()

    on_time = sum(row["on_time"] for row in finishes)
    tardy = len(finishes) - on_time
    unfinished = sum(order["qty"] for order in orders.values()) - len(finishes)
    scheduled = sum(overtime.values())
    actual = sum(row["overtime_hours"] for row in segments)
    return {"ok": True, "on_time_units": on_time, "tardy_units": tardy, "unfinished_units": unfinished,
            "makespan": round(max((row["end"] for row in segments), default=0.0), 4),
            "scheduled_overtime_hours": scheduled, "actual_overtime_hours": actual,
            "accounting_cost": _accounting_cost(scenario, tardy, unfinished, scheduled),
            "breakdown_applied": breakdown, "execution_segments": segments,
            "stage_jobs": stage_jobs, "stage_finishes": stage_finishes, "finishes": finishes}


def _number(value, label, *, positive=False):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{label} must be a finite number")
    try:
        value = float(value)
    except OverflowError as exc:
        raise ValueError(f"{label} must be a finite number") from exc
    if not math.isfinite(value):
        raise ValueError(f"{label} must be a finite number")
    if value < 0 or (positive and value == 0):
        raise ValueError(f"{label} must be {'positive' if positive else 'nonnegative'}")
    return float(value)


def _integer(value, label, upper=None):
    if isinstance(value, bool) or not isinstance(value, int) or value < 0 or (upper is not None and value >= upper):
        raise ValueError(f"{label} must be a nonnegative integer within the declared range")
    return value


def _validate_plan(scenario, plan):
    orders = {row["id"]: row for row in scenario["orders"]}
    machines = {row["id"]: row for row in scenario["machines"]}
    periods = len(scenario["periods"])
    if not isinstance(plan.get("assignment"), list) or not isinstance(plan.get("overtime", []), list):
        raise ValueError("assignment and overtime must be lists")
    overtime = {}
    for row in plan.get("overtime", []):
        if not isinstance(row, dict) or not isinstance(row.get("machine"), str) or row["machine"] not in machines:
            raise ValueError("overtime has an unknown machine")
        period = _integer(row.get("period"), "overtime period", periods)
        hours = _number(row.get("hours"), "overtime hours")
        key = (row["machine"], period)
        if key in overtime:
            raise ValueError("duplicate machine-period overtime authorization")
        if hours > scenario["overtime_cap_hours"] + 1e-9:
            raise ValueError("overtime hours exceed the per-period cap")
        if machines[row["machine"]]["available"][period] == 0 and hours > 0:
            raise ValueError("overtime cannot reopen a zero-availability period")
        overtime[key] = hours
    if "overtime_hours" in plan:
        declared = _number(plan["overtime_hours"], "overtime_hours")
        if not math.isclose(declared, sum(overtime.values()), rel_tol=1e-12, abs_tol=1e-9):
            raise ValueError("overtime_hours disagrees with machine-period authorizations")

    counts, loads = {}, {}
    for row in plan["assignment"]:
        if not isinstance(row, dict) or not isinstance(row.get("order"), str) or row["order"] not in orders:
            raise ValueError("assignment has an unknown order")
        if (row.get("stage") not in scenario["stages"] or not isinstance(row.get("machine"), str)
                or row["machine"] not in machines):
            raise ValueError("assignment has an unknown stage or machine")
        period = _integer(row.get("period"), "assignment period", periods)
        units = _integer(row.get("units"), "assignment units")
        rate = _number(row.get("hours_each"), "assignment hours_each", positive=True)
        expected = orders[row["order"]]["hours"].get(row["stage"], {}).get(row["machine"])
        if expected is None or not math.isclose(rate, expected, rel_tol=1e-12, abs_tol=1e-12):
            raise ValueError("assignment machine/rate is not eligible for this order and stage")
        count_key = (row["order"], row["stage"], period)
        counts[count_key] = counts.get(count_key, 0) + units
        load_key = (row["machine"], period)
        loads[load_key] = loads.get(load_key, 0.0) + units * rate
    for order in orders.values():
        fab, test = 0, 0
        for period in range(periods):
            fab += counts.get((order["id"], "fab", period), 0)
            test += counts.get((order["id"], "test", period), 0)
            if test > fab or fab > order["qty"] or test > order["qty"]:
                raise ValueError("assignment exceeds demand or tests units before their fabrication")
    for (machine, period), hours in loads.items():
        authorized = machines[machine]["available"][period] + overtime.get((machine, period), 0.0)
        if hours > authorized + 1e-9:
            raise ValueError("assignment exceeds machine-period availability and authorized overtime")
    return overtime


def _windows(scenario, overtime, span):
    windows = {}
    for machine in scenario["machines"]:
        for period, available in enumerate(machine["available"]):
            start = period * span + machine["available_start"][period]
            regular_end = start + available
            end = regular_end + overtime.get((machine["id"], period), 0.0) if available > 0 else start
            windows[(machine["id"], period)] = {"start": start, "regular_end": regular_end, "end": end}
    return windows


def _pair_jobs(assignment):
    stages = {}
    for row in assignment:
        bucket = stages.setdefault(row["order"], {"fab": [], "test": []})[row["stage"]]
        for _ in range(row["units"]):
            bucket.append({key: row[key] for key in ("order", "stage", "machine", "period", "hours_each")})
    pairs = []
    for order in sorted(stages):
        grouped = {stage: sorted(rows, key=lambda job: (job["period"], job["machine"], job["hours_each"]))
                   for stage, rows in stages[order].items()}
        for unit, fab in enumerate(grouped["fab"]):
            fab["unit"] = unit
            test = grouped["test"][unit] if unit < len(grouped["test"]) else None
            if test is not None:
                test["unit"] = unit
            pairs.append((fab, test))
    return pairs
