"""Bounded time-indexed HiGHS routing/sequencing of only unstarted operations.

WIP owns its original machine from currentTime through pause/resume completion.
Future starts lie on a configurable grid; exact processing durations are preserved.
Whole ownership intervals are conservatively rasterized, so discretization can
lose feasible schedules but cannot authorize overlapping machine work.
"""
from __future__ import annotations

import math
import numpy as np
from scipy.optimize import Bounds, LinearConstraint, milp
from scipy.sparse import lil_matrix

from mpa.rolling_state import EPS, audit_placements, calendars, horizon, key, validate_snapshot, work
from mpa.simulate import _number


def solve_remaining(scenario, snapshot, events, policy="cost_min", step=0.5):
    scenario = validate_snapshot(scenario, snapshot)
    if policy not in scenario["policies"]:
        raise ValueError("policy must be on the named menu")
    step = _number(step, "grid step", positive=True)
    now, end = snapshot["currentTime"], horizon(scenario)
    if sum(o["qty"] for o in scenario["orders"]) > 32 or math.ceil(end / step) > 256:
        return {"ok": False, "status": "size_limit", "reason": "rolling prototype supports at most 32 units / 256 clock ticks"}
    operations = {key(o): o for o in snapshot["operations"]}
    orders = {o["id"]: o for o in scenario["orders"]}
    windows = calendars(scenario, snapshot["overtime"], events)
    fixed, candidates, groups = [], [], {}

    def placement(identity, machine, start, duration):
        segments, remaining = work(windows[machine], start, duration)
        return {"order": identity[0], "unit": identity[1], "stage": identity[2], "machine": machine,
                "holdStart": start, "segments": segments, "remainingHours": remaining}

    for identity, operation in operations.items():
        if operation["state"] == "wip":
            fixed.append(placement(identity, operation["machine"], now, operation["remainingHours"]))
        elif operation["state"] == "unstarted":
            indices = groups[identity] = []
            for machine, duration in orders[identity[0]]["hours"][identity[2]].items():
                for tick in range(math.ceil((now - EPS) / step), math.ceil(end / step)):
                    start = max(now, tick * step)
                    if not any(w["start"] - EPS <= start < w["end"] - EPS for w in windows[machine]):
                        continue
                    candidate = placement(identity, machine, start, duration)
                    if candidate["remainingHours"] <= EPS:
                        indices.append(len(candidates)); candidates.append(candidate)
                        if len(candidates) > 20000:
                            return {"ok": False, "status": "size_limit", "reason": "rolling candidate budget exceeded"}

    def finish(p):
        return p["segments"][-1]["end"] if p["segments"] and p["remainingHours"] <= EPS else math.inf

    def ticks(p):
        stop = end if p["remainingHours"] > EPS else finish(p)
        return range(max(0, math.floor((p["holdStart"] + EPS) / step)), math.ceil((stop - EPS) / step))

    fixed_finish = {key(p): finish(p) for p in fixed}
    fixed_busy = {(p["machine"], tick) for p in fixed for tick in ticks(p)}
    if not candidates:
        audit_placements(scenario, snapshot, events, fixed)
        return {"ok": True, "status": "no_future_choices", "placements": fixed, "gridStep": step, "mipGap": 0.0}
    rows, lower, upper = [], [], []

    def constraint(coefficients, low=-np.inf, high=np.inf):
        rows.append(coefficients); lower.append(low); upper.append(high)

    for indices in groups.values():
        constraint({i: 1 for i in indices}, high=1)
    occupancy = {}
    for i, candidate in enumerate(candidates):
        for tick in ticks(candidate):
            occupancy.setdefault((candidate["machine"], tick), {})[i] = 1
    for resource, indices in occupancy.items():
        constraint(indices, high=0 if resource in fixed_busy else 1)
    for order in orders.values():
        for unit in range(order["qty"]):
            fab, test = (order["id"], unit, "fab"), (order["id"], unit, "test")
            if test not in groups:
                continue
            if fab in groups:
                # Do not create speculative fabricated inventory when testing
                # cannot also be scheduled. Existing completed/WIP fab is retained.
                constraint({**{i: -1 for i in groups[fab]}, **{i: 1 for i in groups[test]}}, 0, 0)
                coeff = {i: finish(candidates[i]) for i in groups[fab]}
                coeff.update({i: end - candidates[i]["holdStart"] for i in groups[test]})
                constraint(coeff, high=end)
            else:
                predecessor = operations[fab]
                ready = predecessor["completedAt"] if predecessor["state"] == "completed" else fixed_finish.get(fab, math.inf)
                constraint({i: 1 for i in groups[test] if candidates[i]["holdStart"] < ready - EPS}, high=0)
    cost = np.zeros(len(candidates))
    weights = scenario["policies"][policy]
    for i, candidate in enumerate(candidates):
        if candidate["stage"] == "test":
            due = (orders[candidate["order"]]["due"] + 1) * scenario["period_length_hours"]
            cost[i] = (weights["tardiness"] if finish(candidate) > due + EPS else 0) - weights["unfinished"]
    matrix = lil_matrix((len(rows), len(candidates)))
    for r, coefficients in enumerate(rows):
        for i, value in coefficients.items():
            matrix[r, i] = value
    solution = milp(cost, integrality=np.ones(len(candidates)), bounds=Bounds(0, 1),
                    constraints=LinearConstraint(matrix.tocsr(), lower, upper),
                    options={"time_limit": 15.0, "mip_rel_gap": 0.0})
    if not solution.success or solution.x is None:
        return {"ok": False, "status": "solver_limit_or_infeasible", "reason": solution.message}
    selected = fixed + [candidate for candidate, value in zip(candidates, solution.x) if value > 0.5]
    audit_placements(scenario, snapshot, events, selected)
    return {"ok": True, "status": "optimal_on_conservative_grid", "placements": selected,
            "gridStep": step, "mipGap": float(solution.mip_gap), "candidateCount": len(candidates),
            "scope": "future routing/sequencing with frozen authorized overtime; not continuous-time global optimality"}
