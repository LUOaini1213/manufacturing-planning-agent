"""Replay a plan on shared machines. Noise and one breakdown are not in the solver."""

from __future__ import annotations

import random

import simpy

from mpa.solve import _accounting_cost


def simulate(scenario: dict, plan: dict, breakdown: dict | None = None, pace: float | None = None) -> dict:
    if not plan.get("ok"):
        return {"ok": False, "reason": "no plan to simulate"}
    rng = random.Random(int(scenario.get("sim_seed", 0)))
    hours_per_period = float(scenario["hours_per_period"])
    windows = _windows(scenario, plan, breakdown, hours_per_period)
    jobs = _pair_jobs(plan["assignment"])
    env = simpy.Environment()
    resources = {machine["id"]: simpy.Resource(env, capacity=1) for machine in scenario["machines"]}
    finishes: list[dict] = []

    def run_stage(job: dict):
        window = windows[(job["machine"], job["period"])]
        if window["end"] <= window["start"] + 1e-9:
            return None
        # Wait for the opening before taking the machine. A later period must not
        # sit on the resource and block work that is already allowed to start.
        if env.now < window["start"]:
            yield env.timeout(window["start"] - env.now)
        resource = resources[job["machine"]]
        with resource.request() as request:
            yield request
            multiplier = pace if pace is not None else rng.uniform(0.9, 1.15)
            yield env.timeout(job["hours_each"] * multiplier)
            return env.now

    def run_unit(fab: dict, test: dict, order: dict):
        fab_finish = yield from run_stage(fab)
        if fab_finish is None:
            return
        test_finish = yield from run_stage(test)
        if test_finish is None:
            return
        due_at = (order["due"] + 1) * hours_per_period
        finishes.append(
            {
                "order": order["id"],
                "finish": round(test_finish, 4),
                "due_at": due_at,
                "on_time": test_finish <= due_at + 1e-6,
            }
        )

    orders = {order["id"]: order for order in scenario["orders"]}
    for fab, test in jobs:
        env.process(run_unit(fab, test, orders[fab["order"]]))
    env.run()

    produced = len(finishes)
    demand = sum(order["qty"] for order in scenario["orders"])
    on_time = sum(1 for row in finishes if row["on_time"])
    tardy = produced - on_time
    unfinished = demand - produced
    overtime_hours = float(plan.get("overtime_hours") or 0.0)
    return {
        "ok": True,
        "on_time_units": on_time,
        "tardy_units": tardy,
        "unfinished_units": unfinished,
        "makespan": round(max((row["finish"] for row in finishes), default=0.0), 4),
        "accounting_cost": _accounting_cost(scenario, tardy, unfinished, overtime_hours),
        "breakdown_applied": breakdown,
    }


def _windows(scenario: dict, plan: dict, breakdown: dict | None, hours_per_period: float) -> dict:
    overtime = {
        (item["machine"], item["period"]): float(item["hours"]) for item in plan.get("overtime", [])
    }
    windows = {}
    for machine in scenario["machines"]:
        for period, available in enumerate(machine["available"]):
            open_hours = float(available or 0.0) + overtime.get((machine["id"], period), 0.0)
            down = 0.0
            if breakdown and breakdown["machine"] == machine["id"] and breakdown["period"] == period:
                down = min(float(breakdown["hours"]), open_hours)
                open_hours = max(0.0, open_hours - down)
            # The lost hours are a real outage at the start of the period, not a label on the end.
            start = period * hours_per_period + down
            windows[(machine["id"], period)] = {"start": start, "end": start + open_hours}
    return windows


def _pair_jobs(assignment: list[dict]) -> list[tuple[dict, dict]]:
    fab: dict[str, list[dict]] = {}
    test: dict[str, list[dict]] = {}
    for row in assignment:
        bucket = fab if row["stage"] == "fab" else test
        bucket.setdefault(row["order"], [])
        for _ in range(int(row["units"])):
            bucket[row["order"]].append(
                {
                    "order": row["order"],
                    "stage": row["stage"],
                    "machine": row["machine"],
                    "period": row["period"],
                    "hours_each": float(row["hours_each"]),
                }
            )
    pairs = []
    for order in fab:
        fab_jobs = sorted(fab[order], key=lambda job: (job["period"], job["machine"]))
        test_jobs = sorted(test.get(order, []), key=lambda job: (job["period"], job["machine"]))
        pairs.extend(zip(fab_jobs, test_jobs))
    return pairs
