"""Earliest-due-date loading. No solver and no learned policy."""

from __future__ import annotations

from mpa.solve import _accounting_cost


def earliest_due_date(scenario: dict) -> dict:
    if any(hours is None for machine in scenario["machines"] for hours in machine["available"]):
        return {"ok": False, "status": "refused", "reason": "missing availability"}
    orders = sorted(scenario["orders"], key=lambda order: (order["due"], order["id"]))
    machines = scenario["machines"]
    periods = scenario["periods"]
    remaining = {
        (machine["id"], period): float(hours)
        for machine in machines
        for period, hours in enumerate(machine["available"])
    }
    overtime_left = {
        (machine["id"], period): float(scenario["overtime_cap_hours"])
        for machine in machines
        for period in range(len(periods))
    }
    overtime_used = {(machine["id"], period): 0.0 for machine in machines for period in range(len(periods))}
    assignment = []
    per_order = []
    tardy = 0
    unfinished = 0

    for order in orders:
        left = int(order["qty"])
        finished_on_time = 0
        finished_late = 0
        progress = [{"fab": False, "test": False} for _ in range(order["qty"])]
        for period in range(len(periods)):
            for stage in scenario["stages"]:
                options = sorted(
                    (
                        (hours, machine_id)
                        for machine_id, hours in order["hours"].get(stage, {}).items()
                    ),
                    key=lambda item: item[0],
                )
                for unit in range(order["qty"]):
                    if progress[unit][stage]:
                        continue
                    if stage == "test" and not progress[unit]["fab"]:
                        continue
                    placed = False
                    for hours_each, machine_id in options:
                        if _take(remaining, overtime_left, overtime_used, machine_id, period, hours_each):
                            progress[unit][stage] = True
                            assignment.append(
                                {
                                    "order": order["id"],
                                    "stage": stage,
                                    "machine": machine_id,
                                    "period": period,
                                    "period_name": periods[period],
                                    "units": 1,
                                    "hours_each": hours_each,
                                }
                            )
                            placed = True
                            break
                    if not placed:
                        continue
                    if stage == "test":
                        if period <= order["due"]:
                            finished_on_time += 1
                        else:
                            finished_late += 1
                        left -= 1
        unfinished += left
        tardy += finished_late
        per_order.append(
            {
                "order": order["id"],
                "qty": order["qty"],
                "due": order["due"],
                "tardy": finished_late,
                "unfinished": left,
            }
        )

    assignment = _collapse(assignment)
    overtime = [
        {"machine": machine, "period": period, "period_name": periods[period], "hours": round(hours, 4)}
        for (machine, period), hours in sorted(overtime_used.items())
        if hours > 1e-6
    ]
    overtime_hours = round(sum(item["hours"] for item in overtime), 4)
    return {
        "ok": True,
        "status": "heuristic",
        "policy": "earliest_due_date",
        "assignment": assignment,
        "overtime": overtime,
        "orders": per_order,
        "tardy_units": tardy,
        "unfinished_units": unfinished,
        "on_time_units": sum(order["qty"] for order in orders) - tardy - unfinished,
        "overtime_hours": overtime_hours,
        "accounting_cost": _accounting_cost(scenario, tardy, unfinished, overtime_hours),
        "objective": None,
    }


def _take(remaining, overtime_left, overtime_used, machine, period, hours) -> bool:
    key = (machine, period)
    if key not in remaining:
        return False
    if remaining[key] >= hours - 1e-9:
        remaining[key] -= hours
        return True
    gap = hours - remaining[key]
    if overtime_left[key] >= gap - 1e-9:
        overtime_used[key] += gap
        overtime_left[key] -= gap
        remaining[key] = 0.0
        return True
    return False


def _collapse(rows: list[dict]) -> list[dict]:
    grouped: dict[tuple, dict] = {}
    for row in rows:
        key = (row["order"], row["stage"], row["machine"], row["period"])
        if key not in grouped:
            grouped[key] = dict(row)
        else:
            grouped[key]["units"] += row["units"]
    return list(grouped.values())
