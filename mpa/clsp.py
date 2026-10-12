"""Capacitated lot-sizing on the public Trigeiro F1 instance. Not the synthetic two-stage model."""

from __future__ import annotations

import argparse
import json
import math
from collections.abc import Mapping
from numbers import Integral, Real
from pathlib import Path

import numpy as np
from scipy.optimize import Bounds, LinearConstraint, milp

from .reporting import provenance

ROOT = Path(__file__).resolve().parents[1]
F1 = ROOT / "data" / "trigeiro" / "F1.DAT"


def _nonnegative(value, label: str) -> None:
    try:
        valid = (not isinstance(value, bool) and isinstance(value, Real)
                 and math.isfinite(value) and value >= 0)
    except (OverflowError, TypeError, ValueError):
        valid = False
    if not valid:
        raise ValueError(f"{label} must be a finite nonnegative number, not a boolean")


def _sequence(value, length: int, label: str) -> None:
    if (not isinstance(value, (list, tuple, np.ndarray))
            or (isinstance(value, np.ndarray) and value.ndim == 0)
            or len(value) != length):
        raise ValueError(f"{label} must contain exactly {length} entries")


def _vector(value, length: int, label: str) -> None:
    _sequence(value, length, label)
    for i, number in enumerate(value):
        _nonnegative(number, f"{label}[{i}]")


def _matrix_shape(value, rows: int, columns: int, label: str) -> None:
    _sequence(value, rows, label)
    for i, row in enumerate(value):
        _vector(row, columns, f"{label}[{i}]")


def _validate_data(data: dict) -> None:
    if not isinstance(data, Mapping):
        raise ValueError("data must be a model mapping")
    required = ("n_items", "n_periods", "capacity", "unit_time", "holding", "setup_time", "setup_cost", "demand")
    for key in required:
        if key not in data:
            raise ValueError(f"model is missing {key}")
    for key in ("n_items", "n_periods"):
        value = data[key]
        if isinstance(value, bool) or not isinstance(value, Integral) or value <= 0:
            raise ValueError(f"{key} must be a positive integer")
    _nonnegative(data["capacity"], "capacity")
    for key in ("unit_time", "holding", "setup_time", "setup_cost"):
        _vector(data[key], data["n_items"], key)
    _matrix_shape(data["demand"], data["n_periods"], data["n_items"], "demand")


def _finite_sum(values, label: str) -> float:
    try:
        total = math.fsum(values)
    except (OverflowError, ValueError) as exc:
        raise ValueError(f"{label} is not finite") from exc
    if not math.isfinite(total):
        raise ValueError(f"{label} is not finite")
    return total


def _roundoff(*values: float) -> float:
    """IEEE accumulation slack, without a fixed tolerance that erases small lots."""
    return 64 * math.ulp(max(abs(float(value)) for value in values))


def load_trigeiro(path: Path = F1) -> dict:
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        parts = line.split()
        if not parts or not _is_number(parts[0]):
            continue
        try:
            rows.append([float(piece) for piece in parts])
        except ValueError as exc:
            raise ValueError("Trigeiro records must be numeric") from exc
    if len(rows) < 3 or len(rows[0]) != 2 or len(rows[1]) != 1 or len(rows[2]) != 1:
        raise ValueError("Trigeiro header requires two dimensions, one marker and one capacity")
    for value in rows[0]:
        if not math.isfinite(value) or not value.is_integer() or value <= 0:
            raise ValueError("Trigeiro dimensions must be positive integers")
    n_items, n_periods = map(int, rows[0])
    if len(rows) != 3 + n_items + n_periods:
        raise ValueError("Trigeiro record count does not match the declared dimensions")
    if not math.isfinite(rows[1][0]):
        raise ValueError("Trigeiro marker must be finite")
    capacity = float(rows[2][0])
    items = rows[3 : 3 + n_items]
    demand_rows = rows[3 + n_items : 3 + n_items + n_periods]
    if any(len(row) != 4 for row in items):
        raise ValueError("Trigeiro item records must contain exactly four fields")
    if any(len(row) != n_items for row in demand_rows):
        raise ValueError("Trigeiro demand records must contain one field per item")
    data = {
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
    _validate_data(data)
    return data


def solve_clsp(data: dict, *, binary_zero_tolerance: float = 1e-7) -> dict:
    """Solve with zero MIP gap and independently audit the returned quantities.

    binary_zero_tolerance only identifies an inactive solver setup variable.
    Quantity cleanup remains bounded by 64 ULPs of total item demand; this
    parameter never relaxes the independent capacity or demand checks.
    """
    _validate_data(data)
    _nonnegative(binary_zero_tolerance, "binary_zero_tolerance")
    if binary_zero_tolerance >= 0.5:
        raise ValueError("binary_zero_tolerance must be less than 0.5")
    n = data["n_items"]
    periods = data["n_periods"]
    demand = data["demand"]
    big_m = [
        [_finite_sum((demand[k][i] for k in range(t, periods)), "remaining demand") for t in range(periods)]
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
        options={"time_limit": 30.0, "mip_rel_gap": 0.0},
    )
    if not result.success or result.x is None:
        return {"ok": False, "status": int(result.status), "reason": result.message}
    production = _matrix(result.x, x_index, n, periods)
    # HiGHS leaves signed roundoff at zero bounds, including tiny positive x
    # with setup=0. Normalize only this solver boundary, at the demand scale.
    # audit() rejects external negatives and charges every external positive lot.
    for item, lots in enumerate(production):
        for period, qty in enumerate(lots):
            inactive = abs(result.x[setup_index(item, period)]) <= binary_zero_tolerance
            if inactive and abs(qty) <= _roundoff(big_m[item][0]):
                lots[period] = 0.0
    try:
        checked = audit(data, production)
        _nonnegative(result.fun, "solver objective")
        if abs(checked["objective"] - round(float(result.fun), 4)) > 1e-4:
            raise ValueError("solver objective does not match the independent production audit")
    except ValueError as exc:
        return {"ok": False, "status": "invalid_solution", "reason": str(exc)}
    return {
        "ok": True,
        "status": "optimal" if int(result.status) == 0 else int(result.status),
        "objective": round(float(result.fun), 4),
        "mip_gap": getattr(result, "mip_gap", None),
        "setups": int(round(float(result.x[2 * n_x :].sum()))),
        "production": production,
        "inventory": _matrix(result.x, inventory_index, n, periods),
    }


def lot_for_lot(data: dict) -> dict:
    """Place each period's demand in that period. Infeasible if one period cannot hold it."""
    _validate_data(data)
    n = data["n_items"]
    periods = data["n_periods"]
    production = [[0.0 for _ in range(periods)] for _ in range(n)]
    for period, row in enumerate(data["demand"]):
        used = 0.0
        for item, qty in enumerate(row):
            if qty <= 0:
                continue
            used += data["unit_time"][item] * qty + data["setup_time"][item]
            production[item][period] = qty
        if used > data["capacity"] + 1e-9:
            return {"ok": False, "status": "infeasible", "reason": f"period {period} needs {used:g} > {data['capacity']:g}"}
    return _heuristic_result(data, production)


def shift_earlier(data: dict) -> dict:
    """Move an entire item lot into the latest earlier period that can hold it.

    The setup moves with the lot when the destination does not already make that item.
    Partial lots are not split, so a period can stay infeasible even when a smaller move would fit.
    """
    _validate_data(data)
    n = data["n_items"]
    periods = data["n_periods"]
    production = [[float(data["demand"][period][item]) for period in range(periods)] for item in range(n)]
    for _ in range(n * periods * periods * 20):
        loads = [_load(data, production, period) for period in range(periods)]
        over = [period for period, load in enumerate(loads) if load > data["capacity"] + 1e-9]
        if not over:
            return _heuristic_result(data, production)
        period = over[0]
        if period == 0:
            return {"ok": False, "status": "infeasible", "reason": "period 0 still over capacity"}
        moved = False
        candidates = sorted(
            (item for item in range(n) if production[item][period] > 0),
            key=lambda candidate: data["setup_time"][candidate] + data["unit_time"][candidate] * production[candidate][period],
            reverse=True,
        )
        for item in candidates:
            qty = production[item][period]
            for slot in range(period - 1, -1, -1):
                extra_setup = 0.0 if production[item][slot] > 0 else data["setup_time"][item]
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
        if lots[period] > 0:
            total += data["unit_time"][item] * lots[period] + data["setup_time"][item]
    return total


def _heuristic_result(data: dict, production: list[list[float]]) -> dict:
    try:
        checked = audit(data, production)
    except ValueError as exc:
        return {"ok": False, "status": "infeasible", "reason": str(exc)}
    n = data["n_items"]
    periods = data["n_periods"]
    inventory = [[0.0 for _ in range(periods)] for _ in range(n)]
    for item in range(n):
        for period in range(periods):
            level = (math.fsum(production[item][:period + 1])
                     - math.fsum(data["demand"][t][item] for t in range(period + 1)))
            inventory[item][period] = max(0.0, level)
    return {
        "ok": True,
        "status": "heuristic",
        "objective": checked["objective"],
        "setups": sum(qty > 0 for row in production for qty in row),
        "production": production,
        "inventory": inventory,
    }


def merge_earlier(data: dict) -> dict:
    """Fold whole lots into earlier periods that already make the same item.

    First take folds whose setup-cost saving exceeds the extra holding cost.
    If a period is still over capacity, continue with the fold that adds the least
    cost per setup-hour removed. This can drop a setup that lot shifting keeps.
    """
    _validate_data(data)
    n = data["n_items"]
    periods = data["n_periods"]
    production = [[float(data["demand"][period][item]) for period in range(periods)] for item in range(n)]

    def folds() -> list[tuple]:
        found = []
        for item in range(n):
            for period in range(periods):
                qty = production[item][period]
                if qty <= 0:
                    continue
                for slot in range(period):
                    if production[item][slot] <= 0:
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
        apply(min(available, key=lambda row: ((-row[0]) / row[1] if row[1] else math.inf, row[2])))
        guard += 1

    return _heuristic_result(data, production)


def coordinate_plan(data: dict) -> dict:
    """Improve a feasible merge by replanning one item at a time, then producing later.

    Each item is solved against the capacity the others leave behind. Production that can
    move into a later period is moved when the holding saved exceeds any new setup.
    A later pass moves one lot off a tight period and replans the other items first, so
    freed capacity can remove one of their setups. The MIP solution is not an input.
    Demand must be integer-valued; the other planners support continuous demand.
    """
    _validate_data(data)
    for row in data["demand"]:
        for qty in row:
            _whole(qty)
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

    def delay_until_stable() -> None:
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
                if unit == 0:
                    continue
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
                return
            _, item, src, dest, qty = best
            production[item][src] = max(0.0, production[item][src] - qty)
            production[item][dest] += qty

    def descend(trial: list[list[float]], order: list[int]) -> list[list[float]]:
        trial = [row[:] for row in trial]
        for _ in range(n):
            changed = False
            saved = [row[:] for row in production]
            production[:] = trial
            for item in order:
                lots = replan(item)
                if lots is not None and any(abs(lots[period] - trial[item][period]) > 1e-6 for period in range(periods)):
                    trial[item] = lots
                    production[:] = trial
                    changed = True
            production[:] = saved
            if not changed:
                break
        return trial

    def kick_tight_lots() -> None:
        for _ in range(n):
            loads = [_load(data, production, period) for period in range(periods)]
            tight = [period for period, load in enumerate(loads) if load >= 680]
            slack = [period for period, load in enumerate(loads) if data["capacity"] - load >= 30]
            chosen = None
            for item in range(n):
                for src in tight:
                    qty = production[item][src]
                    if qty <= 1e-9:
                        continue
                    for dest in slack:
                        if dest == src:
                            continue
                        unit = data["unit_time"][item]
                        setup_time = data["setup_time"][item]
                        already = production[item][dest] > 1e-9
                        added = unit * qty + (0.0 if already else setup_time)
                        if _load(data, production, dest) + added > data["capacity"] + 1e-6:
                            continue
                        if dest > src:
                            on_hand = 0.0
                            spare = float("inf")
                            for period in range(periods):
                                on_hand += production[item][period] - data["demand"][period][item]
                                if src <= period < dest:
                                    spare = min(spare, on_hand)
                            if spare + 1e-8 < qty:
                                continue
                        trial = [row[:] for row in production]
                        trial[item][src] = 0.0
                        trial[item][dest] += qty
                        order = [other for other in range(n) if other != item] + [item]
                        trial = descend(trial, order)
                        try:
                            cost = audit(data, trial)["objective"]
                        except ValueError:
                            continue
                        if chosen is None or cost < chosen[0] - 1e-9:
                            chosen = (cost, trial)
            current = audit(data, production)["objective"]
            if chosen is None or chosen[0] >= current - 1e-6:
                return
            production[:] = chosen[1]

    delay_until_stable()
    kick_tight_lots()
    delay_until_stable()

    return _heuristic_result(data, production)


def _whole(value: float) -> int:
    rounded = round(value)
    if abs(value - rounded) > _roundoff(value, rounded):
        raise ValueError("coordinate plan expects integer demand")
    return int(rounded)


def audit(data: dict, production: list[list[float]]) -> dict:
    """Rebuild inventory and cost from quantities. Independent of the solver matrix."""
    _validate_data(data)
    n = data["n_items"]
    periods = data["n_periods"]
    _matrix_shape(production, n, periods, "production")
    cost_terms = []
    loads = []
    for period in range(periods):
        load_terms = []
        for item in range(n):
            qty = production[item][period]
            if qty > 0:
                load_terms.extend((data["unit_time"][item] * qty, data["setup_time"][item]))
                cost_terms.append(data["setup_cost"][item])
        load = _finite_sum(load_terms, f"period {period} load")
        loads.append(load)
        if load - data["capacity"] > _roundoff(load, data["capacity"]):
            raise ValueError(f"period {period} load {load:g} exceeds {data['capacity']:g}")
    for item in range(n):
        for period in range(periods):
            produced = _finite_sum(production[item][:period + 1], f"item {item} production")
            needed = _finite_sum((data["demand"][t][item] for t in range(period + 1)), f"item {item} demand")
            level = produced - needed
            if level < -_roundoff(produced, needed):
                raise ValueError(f"item {item} period {period} short by {-level:g}")
            cost_terms.append(data["holding"][item] * max(0.0, level))
    cost = _finite_sum(cost_terms, "objective")
    return {"objective": round(cost, 4), "loads": [round(load, 4) for load in loads]}


def _matrix(values: np.ndarray, index, n: int, periods: int) -> list[list[float]]:
    return [[float(values[index(item, period)]) for period in range(periods)] for item in range(n)]


def _is_number(token: str) -> bool:
    try:
        float(token)
    except ValueError:
        return False
    return True


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=ROOT / "results")
    args = parser.parse_args()
    data = load_trigeiro()
    optimal = solve_clsp(data)
    direct = lot_for_lot(data)
    shifted = shift_earlier(data)
    merged = merge_earlier(data)
    coordinated = coordinate_plan(data)
    report = {
        "instance": "Trigeiro F1",
        "citation": "Trigeiro, Thomas, McClain, Management Science 35(3):353-366, 1989",
        "mirror": "https://github.com/gsamaro/trigeiro_fdata/blob/a54b0fb51657fb93530b60faa794893e3d0bfae8/data/F1.DAT",
        "items": data["n_items"],
        "periods": data["n_periods"],
        "capacity": data["capacity"],
        "provenance": provenance([F1]),
        "highs": {key: optimal[key] for key in ("ok", "status", "objective", "setups", "mip_gap", "reason") if key in optimal},
        "lot_for_lot": {key: direct[key] for key in ("ok", "status", "objective", "setups", "reason") if key in direct},
        "shift_earlier": {key: shifted[key] for key in ("ok", "status", "objective", "setups", "reason") if key in shifted},
        "merge_earlier": {key: merged[key] for key in ("ok", "status", "objective", "setups", "reason") if key in merged},
        "coordinate_plan": {key: coordinated[key] for key in ("ok", "status", "objective", "setups", "reason") if key in coordinated},
    }
    out = args.output_dir / "trigeiro_f1.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
