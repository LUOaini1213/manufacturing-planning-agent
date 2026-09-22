"""Capacity MIP. HiGHS is the solver; the model does not invent due dates or hours."""

from __future__ import annotations

import numpy as np
from scipy.optimize import Bounds, LinearConstraint, milp

from mpa.scenario import missing_availability


def solve(scenario: dict, policy: str = "cost_min") -> dict:
    missing = missing_availability(scenario)
    if missing:
        return {"ok": False, "status": "refused", "reason": "missing availability: " + ", ".join(missing)}
    if policy not in scenario["policies"]:
        return {"ok": False, "status": "refused", "reason": f"policy {policy} is not on the menu"}

    orders = scenario["orders"]
    machines = scenario["machines"]
    stages = scenario["stages"]
    periods = scenario["periods"]
    n_i, n_s, n_m, n_t = len(orders), len(stages), len(machines), len(periods)
    n_x = n_i * n_s * n_m * n_t
    n_o = n_m * n_t
    n_late = n_i
    n_unf = n_i
    n = n_x + n_o + n_late + n_unf
    fab, test = 0, 1

    def x_index(order: int, stage: int, machine: int, period: int) -> int:
        return ((order * n_s + stage) * n_m + machine) * n_t + period

    def overtime_index(machine: int, period: int) -> int:
        return n_x + machine * n_t + period

    def late_index(order: int) -> int:
        return n_x + n_o + order

    def unfinished_index(order: int) -> int:
        return n_x + n_o + n_late + order

    weights = scenario["policies"][policy]
    cost = np.zeros(n)
    for machine in range(n_m):
        for period in range(n_t):
            cost[overtime_index(machine, period)] = weights["overtime"]
    for order in range(n_i):
        cost[late_index(order)] = weights["tardiness"]
        cost[unfinished_index(order)] = weights["unfinished"]

    integrality = np.zeros(n)
    integrality[:n_x] = 1
    integrality[n_x + n_o :] = 1
    lower = np.zeros(n)
    upper = np.full(n, np.inf)
    cap = float(scenario["overtime_cap_hours"])
    for machine in range(n_m):
        for period in range(n_t):
            upper[overtime_index(machine, period)] = cap
    for i, order in enumerate(orders):
        upper[late_index(i)] = order["qty"]
        upper[unfinished_index(i)] = order["qty"]
        for s, stage in enumerate(stages):
            for m, machine in enumerate(machines):
                eligible = machine["id"] in order["hours"].get(stage, {})
                for period in range(n_t):
                    upper[x_index(i, s, m, period)] = order["qty"] if eligible else 0

    rows: list[tuple[np.ndarray, float, float]] = []

    def add(row: np.ndarray, low: float, high: float) -> None:
        rows.append((row, low, high))

    for i, order in enumerate(orders):
        balance = np.zeros(n)
        for m in range(n_m):
            for period in range(n_t):
                balance[x_index(i, test, m, period)] = 1
        balance[unfinished_index(i)] = 1
        add(balance, order["qty"], order["qty"])

        matched = np.zeros(n)
        for m in range(n_m):
            for period in range(n_t):
                matched[x_index(i, fab, m, period)] = 1
                matched[x_index(i, test, m, period)] = -1
        add(matched, 0, 0)

        for period in range(n_t):
            precedence = np.zeros(n)
            for earlier in range(period + 1):
                for m in range(n_m):
                    precedence[x_index(i, fab, m, earlier)] = 1
                    precedence[x_index(i, test, m, earlier)] = -1
            add(precedence, 0, np.inf)

        late = np.zeros(n)
        late[late_index(i)] = 1
        for m in range(n_m):
            for period in range(n_t):
                if period > order["due"]:
                    late[x_index(i, test, m, period)] = -1
        add(late, 0, np.inf)

    for m, machine in enumerate(machines):
        for period in range(n_t):
            capacity = np.zeros(n)
            for i, order in enumerate(orders):
                for s, stage in enumerate(stages):
                    hours = float(order["hours"].get(stage, {}).get(machine["id"], 0.0))
                    capacity[x_index(i, s, m, period)] = hours
            capacity[overtime_index(m, period)] = -1
            add(capacity, -np.inf, float(machine["available"][period]))

    matrix = np.vstack([row for row, _, _ in rows])
    result = milp(
        cost,
        integrality=integrality,
        bounds=Bounds(lower, upper),
        constraints=LinearConstraint(
            matrix,
            np.array([low for _, low, _ in rows], dtype=float),
            np.array([high for _, _, high in rows], dtype=float),
        ),
        options={"time_limit": 10.0},
    )
    status = _status_name(int(result.status))
    if not result.success or result.x is None:
        return {"ok": False, "status": status, "reason": result.message, "policy": policy}

    assignment = []
    for i, order in enumerate(orders):
        for s, stage in enumerate(stages):
            for m, machine in enumerate(machines):
                for period, period_name in enumerate(periods):
                    units = int(round(result.x[x_index(i, s, m, period)]))
                    if units:
                        assignment.append(
                            {
                                "order": order["id"],
                                "stage": stage,
                                "machine": machine["id"],
                                "period": period,
                                "period_name": period_name,
                                "units": units,
                                "hours_each": float(order["hours"][stage][machine["id"]]),
                            }
                        )
    overtime = []
    for m, machine in enumerate(machines):
        for period, period_name in enumerate(periods):
            hours = float(result.x[overtime_index(m, period)])
            if hours > 1e-6:
                overtime.append({"machine": machine["id"], "period": period, "period_name": period_name, "hours": round(hours, 4)})
    per_order = []
    tardy = 0
    unfinished = 0
    for i, order in enumerate(orders):
        late_units = int(round(result.x[late_index(i)]))
        unfinished_units = int(round(result.x[unfinished_index(i)]))
        tardy += late_units
        unfinished += unfinished_units
        per_order.append(
            {
                "order": order["id"],
                "qty": order["qty"],
                "due": order["due"],
                "tardy": late_units,
                "unfinished": unfinished_units,
            }
        )
    return {
        "ok": True,
        "status": status,
        "policy": policy,
        "objective": round(float(result.fun), 4),
        "mip_gap": getattr(result, "mip_gap", None),
        "assignment": assignment,
        "overtime": overtime,
        "orders": per_order,
        "tardy_units": tardy,
        "unfinished_units": unfinished,
        "on_time_units": sum(order["qty"] for order in orders) - tardy - unfinished,
        "overtime_hours": round(sum(item["hours"] for item in overtime), 4),
        "accounting_cost": _accounting_cost(scenario, tardy, unfinished, sum(item["hours"] for item in overtime)),
    }


def _accounting_cost(scenario: dict, tardy: int, unfinished: int, overtime_hours: float) -> float:
    costs = scenario["costs"]
    return round(
        overtime_hours * costs["overtime_per_hour"]
        + tardy * costs["tardiness_per_unit"]
        + unfinished * costs["unfinished_per_unit"],
        4,
    )


def _status_name(code: int) -> str:
    return {0: "optimal", 1: "iteration_limit", 2: "infeasible", 3: "unbounded", 4: "other"}.get(code, str(code))
