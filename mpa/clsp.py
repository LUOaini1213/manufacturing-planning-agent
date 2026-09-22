"""Capacitated lot-sizing on the public Trigeiro F1 instance. Not the synthetic two-stage model."""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
from scipy.optimize import Bounds, LinearConstraint, milp

ROOT = Path(__file__).resolve().parents[1]
F1 = ROOT / "data" / "trigeiro" / "F1.DAT"


def load_trigeiro(path: Path = F1) -> dict:
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        parts = line.split()
        if not parts or not _is_number(parts[0]):
            continue
        rows.append([float(piece) for piece in parts])
    n_items = int(rows[0][0])
    n_periods = int(rows[0][1])
    capacity = float(rows[2][0])
    items = rows[3 : 3 + n_items]
    demand_rows = rows[3 + n_items : 3 + n_items + n_periods]
    if len(items) != n_items or len(demand_rows) != n_periods:
        raise ValueError(f"F1 parse failed: items={len(items)} periods={len(demand_rows)}")
    return {
        "name": path.name,
        "n_items": n_items,
        "n_periods": n_periods,
        "capacity": capacity,
        "unit_time": [row[0] for row in items],
        "holding": [row[1] for row in items],
        "setup_time": [row[2] for row in items],
        "setup_cost": [row[3] for row in items],
        "demand": [[row[i] for i in range(n_items)] for row in demand_rows],
    }


def solve_clsp(data: dict) -> dict:
    n = data["n_items"]
    periods = data["n_periods"]
    demand = data["demand"]
    big_m = [
        [sum(demand[k][i] for k in range(t, periods)) for t in range(periods)]
        for i in range(n)
    ]
    n_x = n * periods
    size = 3 * n_x

    def x_index(item: int, period: int) -> int:
        return item * periods + period

    def inventory_index(item: int, period: int) -> int:
        return n_x + item * periods + period

    def setup_index(item: int, period: int) -> int:
        return 2 * n_x + item * periods + period

    cost = np.zeros(size)
    for item in range(n):
        for period in range(periods):
            cost[inventory_index(item, period)] = data["holding"][item]
            cost[setup_index(item, period)] = data["setup_cost"][item]
    integrality = np.zeros(size)
    integrality[2 * n_x :] = 1
    lower = np.zeros(size)
    upper = np.full(size, np.inf)
    upper[2 * n_x :] = 1

    rows = []

    def add(row: np.ndarray, low: float, high: float) -> None:
        rows.append((row, low, high))

    for item in range(n):
        for period in range(periods):
            balance = np.zeros(size)
            balance[inventory_index(item, period)] = 1
            balance[x_index(item, period)] = -1
            if period:
                balance[inventory_index(item, period - 1)] = -1
            add(balance, -demand[period][item], -demand[period][item])
            link = np.zeros(size)
            link[x_index(item, period)] = 1
            link[setup_index(item, period)] = -big_m[item][period]
            add(link, -np.inf, 0)
    for period in range(periods):
        capacity = np.zeros(size)
        for item in range(n):
            capacity[x_index(item, period)] = data["unit_time"][item]
            capacity[setup_index(item, period)] = data["setup_time"][item]
        add(capacity, -np.inf, data["capacity"])

    result = milp(
        cost,
        integrality=integrality,
        bounds=Bounds(lower, upper),
        constraints=LinearConstraint(
            np.vstack([row for row, _, _ in rows]),
            np.array([low for _, low, _ in rows], dtype=float),
            np.array([high for _, _, high in rows], dtype=float),
        ),
        options={"time_limit": 30.0},
    )
    if not result.success or result.x is None:
        return {"ok": False, "status": int(result.status), "reason": result.message}
    return {
        "ok": True,
        "status": "optimal" if int(result.status) == 0 else int(result.status),
        "objective": round(float(result.fun), 4),
        "mip_gap": getattr(result, "mip_gap", None),
        "setups": int(round(float(result.x[2 * n_x :].sum()))),
        "production": _matrix(result.x, x_index, n, periods),
        "inventory": _matrix(result.x, inventory_index, n, periods),
    }


def lot_for_lot(data: dict) -> dict:
    """Place each period's demand in that period. Infeasible if one period cannot hold it."""
    n = data["n_items"]
    periods = data["n_periods"]
    production = [[0.0 for _ in range(periods)] for _ in range(n)]
    setups = [[0 for _ in range(periods)] for _ in range(n)]
    for period, row in enumerate(data["demand"]):
        used = 0.0
        for item, qty in enumerate(row):
            if qty <= 0:
                continue
            used += data["unit_time"][item] * qty + data["setup_time"][item]
            production[item][period] = qty
            setups[item][period] = 1
        if used > data["capacity"] + 1e-9:
            return {"ok": False, "status": "infeasible", "reason": f"period {period} needs {used:g} > {data['capacity']:g}"}
    return {"ok": True, "status": "heuristic", **_cost_of(data, production, setups)}


def shift_earlier(data: dict) -> dict:
    """Move an entire item lot into the latest earlier period that can hold it.

    The setup moves with the lot when the destination does not already make that item.
    Partial lots are not split, so a period can stay infeasible even when a smaller move would fit.
    """
    n = data["n_items"]
    periods = data["n_periods"]
    production = [[float(data["demand"][period][item]) for period in range(periods)] for item in range(n)]
    for _ in range(n * periods * periods * 20):
        loads = [_load(data, production, period) for period in range(periods)]
        over = [period for period, load in enumerate(loads) if load > data["capacity"] + 1e-9]
        if not over:
            setups = [[1 if production[item][period] > 1e-9 else 0 for period in range(periods)] for item in range(n)]
            return {"ok": True, "status": "heuristic", **_cost_of(data, production, setups)}
        period = over[0]
        if period == 0:
            return {"ok": False, "status": "infeasible", "reason": "period 0 still over capacity"}
        moved = False
        candidates = sorted(
            (item for item in range(n) if production[item][period] > 1e-9),
            key=lambda candidate: data["setup_time"][candidate] + data["unit_time"][candidate] * production[candidate][period],
            reverse=True,
        )
        for item in candidates:
            qty = production[item][period]
            for slot in range(period - 1, -1, -1):
                extra_setup = 0.0 if production[item][slot] > 1e-9 else data["setup_time"][item]
                added = data["unit_time"][item] * qty + extra_setup
                if _load(data, production, slot) + added <= data["capacity"] + 1e-9:
                    production[item][slot] += qty
                    production[item][period] = 0.0
                    moved = True
                    break
            if moved:
                break
        if not moved:
            return {"ok": False, "status": "infeasible", "reason": f"no earlier slot fits a lot from period {period}"}
    return {"ok": False, "status": "infeasible", "reason": "shift limit reached"}


def _load(data: dict, production: list[list[float]], period: int) -> float:
    total = 0.0
    for item, lots in enumerate(production):
        if lots[period] > 1e-9:
            total += data["unit_time"][item] * lots[period] + data["setup_time"][item]
    return total


def _cost_of(data: dict, production: list[list[float]], setups: list[list[int]]) -> dict:
    n = data["n_items"]
    periods = data["n_periods"]
    inventory = [[0.0 for _ in range(periods)] for _ in range(n)]
    cost = 0.0
    for item in range(n):
        previous = 0.0
        for period in range(periods):
            level = previous + production[item][period] - data["demand"][period][item]
            if level < -1e-6:
                raise ValueError("heuristic inventory went negative")
            inventory[item][period] = max(0.0, level)
            previous = inventory[item][period]
            cost += data["setup_cost"][item] * setups[item][period]
            cost += data["holding"][item] * inventory[item][period]
    return {
        "objective": round(cost, 4),
        "setups": int(sum(sum(row) for row in setups)),
        "production": production,
        "inventory": inventory,
    }


def merge_earlier(data: dict) -> dict:
    """Fold whole lots into earlier periods that already make the same item.

    First take folds whose setup-cost saving exceeds the extra holding cost.
    If a period is still over capacity, continue with the fold that adds the least
    cost per setup-hour removed. This can drop a setup that lot shifting keeps.
    """
    n = data["n_items"]
    periods = data["n_periods"]
    production = [[float(data["demand"][period][item]) for period in range(periods)] for item in range(n)]

    def folds() -> list[tuple]:
        found = []
        for item in range(n):
            for period in range(periods):
                qty = production[item][period]
                if qty <= 1e-9:
                    continue
                for slot in range(period):
                    if production[item][slot] <= 1e-9:
                        continue
                    added = data["unit_time"][item] * qty
                    if _load(data, production, slot) + added > data["capacity"] + 1e-6:
                        continue
                    extra_holding = data["holding"][item] * qty * (period - slot)
                    saved_cost = data["setup_cost"][item]
                    found.append(
                        (
                            saved_cost - extra_holding,
                            data["setup_time"][item],
                            extra_holding,
                            item,
                            slot,
                            period,
                            qty,
                        )
                    )
        return found

    def apply(choice: tuple) -> None:
        _, _, _, item, slot, period, qty = choice
        production[item][slot] += qty
        production[item][period] = 0.0

    while True:
        saving = [row for row in folds() if row[0] > 1e-6]
        if not saving:
            break
        apply(max(saving, key=lambda row: (row[0], row[1])))

    guard = 0
    while True:
        over = [period for period in range(periods) if _load(data, production, period) > data["capacity"] + 1e-6]
        if not over:
            break
        available = folds()
        if not available or guard > n * periods * periods:
            period = over[0]
            used = _load(data, production, period)
            return {
                "ok": False,
                "status": "infeasible",
                "reason": f"period {period} still needs {used:g} > {data['capacity']:g}",
            }
        apply(min(available, key=lambda row: ((-row[0]) / row[1], row[2])))
        guard += 1

    setups = [[1 if production[item][period] > 1e-9 else 0 for period in range(periods)] for item in range(n)]
    return {"ok": True, "status": "heuristic", **_cost_of(data, production, setups)}


def coordinate_plan(data: dict) -> dict:
    """Improve a feasible merge by replanning one item at a time, then producing later.

    Each item is solved against the capacity the others leave behind. Production that can
    move into a later period is moved when the holding saved exceeds any new setup.
    """
    merged = merge_earlier(data)
    if not merged["ok"]:
        return merged
    n = data["n_items"]
    periods = data["n_periods"]
    production = [row[:] for row in merged["production"]]

    def others(item: int, period: int) -> float:
        total = 0.0
        for other, lots in enumerate(production):
            if other != item and lots[period] > 1e-9:
                total += data["unit_time"][other] * lots[period] + data["setup_time"][other]
        return total

    def replan(item: int) -> list[float] | None:
        demand = [_whole(data["demand"][period][item]) for period in range(periods)]
        total = sum(demand)
        unit = data["unit_time"][item]
        setup_time = data["setup_time"][item]
        holding = data["holding"][item]
        setup_cost = data["setup_cost"][item]
        cost = [{0: 0.0}] + [dict() for _ in range(periods)]
        previous = [{} for _ in range(periods + 1)]
        for period, need in enumerate(demand):
            residual = data["capacity"] - others(item, period)
            max_qty = int((residual - setup_time) // unit) if unit and residual > setup_time else 0
            for on_hand, paid in cost[period].items():
                choices = [0] if on_hand >= need else []
                running = 0
                quantities = []
                for later in range(period, periods):
                    running += demand[later]
                    quantities.append(running - on_hand)
                quantities.append(max_qty)
                for qty in quantities:
                    if 0 < qty <= max_qty and unit * qty + setup_time <= residual + 1e-6:
                        choices.append(int(qty))
                seen = set()
                for qty in choices:
                    if qty in seen:
                        continue
                    seen.add(qty)
                    nxt = on_hand + qty - need
                    if nxt < 0 or nxt > total:
                        continue
                    paid_next = paid + (setup_cost if qty else 0) + holding * nxt
                    old = cost[period + 1].get(nxt)
                    if old is None or paid_next < old - 1e-9:
                        cost[period + 1][nxt] = paid_next
                        previous[period + 1][nxt] = (on_hand, qty)
        if 0 not in cost[periods]:
            return None
        lots = [0.0] * periods
        on_hand = 0
        for period in range(periods, 0, -1):
            on_hand, qty = previous[period][on_hand]
            lots[period - 1] = float(qty)
        return lots

    for _ in range(n):
        changed = False
        order = sorted(range(n), key=lambda item: data["setup_cost"][item] / max(data["holding"][item], 1e-9), reverse=True)
        for item in order:
            lots = replan(item)
            if lots is not None and any(abs(lots[period] - production[item][period]) > 1e-6 for period in range(periods)):
                production[item] = lots
                changed = True
        if not changed:
            break

    for _ in range(n * periods):
        levels = []
        for item in range(n):
            on_hand = 0.0
            row = []
            for period in range(periods):
                on_hand += production[item][period] - data["demand"][period][item]
                row.append(on_hand)
            levels.append(row)
        best = None
        for item in range(n):
            holding = data["holding"][item]
            unit = data["unit_time"][item]
            setup_time = data["setup_time"][item]
            setup_cost = data["setup_cost"][item]
            for src in range(periods):
                here = production[item][src]
                if here <= 1e-9:
                    continue
                for dest in range(src + 1, periods):
                    spare = min(levels[item][period] for period in range(src, dest))
                    if spare <= 1e-8:
                        break
                    room = data["capacity"] - _load(data, production, dest)
                    already = production[item][dest] > 1e-9
                    fit = room / unit if already else ((room - setup_time) / unit if room > setup_time else 0.0)
                    qty_cap = min(here, spare, fit)
                    if qty_cap <= 1e-8:
                        continue
                    for qty in {qty_cap, here}:
                        if qty <= 1e-8 or qty > qty_cap + 1e-8:
                            continue
                        full = qty >= here - 1e-8
                        gain = holding * qty * (dest - src)
                        if full and already:
                            gain += setup_cost
                        elif not full and not already:
                            gain -= setup_cost
                        if gain > 1e-6 and (best is None or gain > best[0]):
                            best = (gain, item, src, dest, here if full else qty)
        if best is None:
            break
        _, item, src, dest, qty = best
        production[item][src] = max(0.0, production[item][src] - qty)
        production[item][dest] += qty

    try:
        audit(data, production)
    except ValueError as exc:
        return {"ok": False, "status": "infeasible", "reason": str(exc)}
    setups = [[1 if production[item][period] > 1e-9 else 0 for period in range(periods)] for item in range(n)]
    return {"ok": True, "status": "heuristic", **_cost_of(data, production, setups)}


def _whole(value: float) -> int:
    rounded = round(value)
    if abs(value - rounded) > 1e-6:
        raise ValueError("coordinate plan expects integer demand")
    return int(rounded)


def audit(data: dict, production: list[list[float]]) -> dict:
    """Rebuild inventory and cost from quantities. Independent of the solver matrix."""
    n = data["n_items"]
    periods = data["n_periods"]
    inventory = [[0.0 for _ in range(periods)] for _ in range(n)]
    cost = 0.0
    loads = []
    for period in range(periods):
        load = 0.0
        for item in range(n):
            qty = production[item][period]
            if qty > 1e-6:
                load += data["unit_time"][item] * qty + data["setup_time"][item]
                cost += data["setup_cost"][item]
        loads.append(load)
        if load > data["capacity"] + 1e-4:
            raise ValueError(f"period {period} load {load:g} exceeds {data['capacity']:g}")
    for item in range(n):
        previous = 0.0
        for period in range(periods):
            level = previous + production[item][period] - data["demand"][period][item]
            if level < -1e-4:
                raise ValueError(f"item {item} period {period} short by {-level:g}")
            inventory[item][period] = max(0.0, level)
            previous = inventory[item][period]
            cost += data["holding"][item] * inventory[item][period]
    return {"objective": round(cost, 4), "loads": [round(load, 4) for load in loads]}


def _matrix(values: np.ndarray, index, n: int, periods: int) -> list[list[float]]:
    return [[round(float(values[index(item, period)]), 4) for period in range(periods)] for item in range(n)]


def _is_number(token: str) -> bool:
    try:
        float(token)
    except ValueError:
        return False
    return True


def main() -> None:
    data = load_trigeiro()
    optimal = solve_clsp(data)
    direct = lot_for_lot(data)
    shifted = shift_earlier(data)
    merged = merge_earlier(data)
    coordinated = coordinate_plan(data)
    report = {
        "instance": "Trigeiro F1",
        "citation": "Trigeiro, Thomas, McClain, Management Science 35(3):353-366, 1989",
        "mirror": "https://github.com/gsamaro/trigeiro_fdata/blob/main/data/F1.DAT",
        "items": data["n_items"],
        "periods": data["n_periods"],
        "capacity": data["capacity"],
        "highs": {key: optimal[key] for key in ("ok", "status", "objective", "setups", "mip_gap") if key in optimal},
        "lot_for_lot": {key: direct[key] for key in ("ok", "status", "objective", "setups", "reason") if key in direct},
        "shift_earlier": {key: shifted[key] for key in ("ok", "status", "objective", "setups", "reason") if key in shifted},
        "merge_earlier": {key: merged[key] for key in ("ok", "status", "objective", "setups", "reason") if key in merged},
        "coordinate_plan": {key: coordinated[key] for key in ("ok", "status", "objective", "setups", "reason") if key in coordinated},
    }
    out = ROOT / "results" / "trigeiro_f1.json"
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
