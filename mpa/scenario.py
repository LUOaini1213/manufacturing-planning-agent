"""Load a planning scenario. Null availability is missing data, not zero."""

from __future__ import annotations

import json
import math
from copy import deepcopy
from pathlib import Path


def load(path: str | Path) -> dict:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    return normalize(data)


def normalize(data: dict) -> dict:
    """Validate the two-stage scenario and return an independent copy with defaults.

    None availability remains unknown. Numeric strings and booleans are not hours,
    quantities or costs, even though Python can coerce them into numbers.
    """
    _mapping(data, "scenario")
    scenario = deepcopy(data)
    scenario.setdefault("request", "")
    scenario.setdefault("sim_breakdown", None)
    scenario.setdefault("sim_seed", 0)
    if scenario["request"] is not None and not isinstance(scenario["request"], str):
        raise ValueError("request must be a string or null")
    _integer(scenario["sim_seed"], "sim_seed")

    periods = _list(scenario.get("periods"), "periods", nonempty=True)
    _unique_ids(periods, "periods")
    if scenario.get("stages") != ["fab", "test"]:
        raise ValueError("stages must be ['fab', 'test'] in that order")
    scenario["hours_per_period"] = _number(scenario.get("hours_per_period"), "hours_per_period")
    scenario["overtime_cap_hours"] = _number(scenario.get("overtime_cap_hours"), "overtime_cap_hours")
    scenario["period_length_hours"] = period_length(scenario)

    costs = _mapping(scenario.get("costs"), "costs")
    for key in ("overtime_per_hour", "tardiness_per_unit", "unfinished_per_unit"):
        costs[key] = _number(costs.get(key), f"costs.{key}")
    policies = _mapping(scenario.get("policies"), "policies")
    if not policies:
        raise ValueError("policies must contain at least one named policy")
    for name, policy in policies.items():
        _identifier(name, "policy name")
        _mapping(policy, f"policies.{name}")
        for key in ("overtime", "tardiness", "unfinished"):
            policy[key] = _number(policy.get(key), f"policies.{name}.{key}")

    machines = _list(scenario.get("machines"), "machines", nonempty=True)
    for machine in machines:
        _mapping(machine, "machine")
    machine_ids = _unique_ids([machine.get("id") for machine in machines], "machine id")
    for machine in machines:
        prefix = f"machine {machine['id']}"
        available = _period_values(machine.get("available"), len(periods), f"{prefix}.available")
        starts = _period_values(machine.setdefault("available_start", [0.0] * len(periods)), len(periods), f"{prefix}.available_start")
        machine["available"] = [None if value is None else _number(value, f"{prefix}.available[{index}]")
                                for index, value in enumerate(available)]
        machine["available_start"] = [_number(value, f"{prefix}.available_start[{index}]")
                                      for index, value in enumerate(starts)]
        for start, hours in zip(machine["available_start"], machine["available"]):
            end = start + (hours if hours is not None else 0)
            shift = scenario["hours_per_period"]
            if end > shift and not math.isclose(end, shift, rel_tol=1e-12, abs_tol=1e-12):
                raise ValueError(f"{prefix} regular window must fit hours_per_period")

    orders = _list(scenario.get("orders"), "orders", nonempty=True)
    for order in orders:
        _mapping(order, "order")
    _unique_ids([order.get("id") for order in orders], "order id")
    for order in orders:
        prefix = f"order {order['id']}"
        _integer(order.get("qty"), f"{prefix}.qty")
        due = _integer(order.get("due"), f"{prefix}.due")
        if due >= len(periods):
            raise ValueError(f"{prefix}.due must be a period index")
        hours = _mapping(order.get("hours"), f"{prefix}.hours")
        if set(hours) != {"fab", "test"}:
            raise ValueError(f"{prefix}.hours must specify fab and test")
        for stage, rates in hours.items():
            _mapping(rates, f"{prefix}.hours.{stage}")
            for machine_id, value in rates.items():
                _identifier(machine_id, f"{prefix} machine id")
                if machine_id not in machine_ids:
                    raise ValueError(f"{prefix} refers to unknown machine {machine_id}")
                rates[machine_id] = _number(value, f"{prefix}.hours.{stage}.{machine_id}", positive=True)
    _check_breakdown(scenario, scenario["sim_breakdown"])
    return scenario


def validate(scenario: dict) -> None:
    """Raise ValueError for invalid input, without altering the caller's data."""
    normalize(scenario)


def period_length(scenario: dict) -> float:
    """Clock spacing between periods; regular shift plus overtime must fit."""
    _mapping(scenario, "scenario")
    shift = _number(scenario.get("hours_per_period"), "hours_per_period")
    overtime = _number(scenario.get("overtime_cap_hours"), "overtime_cap_hours")
    minimum = _number(shift + overtime, "hours_per_period + overtime_cap_hours")
    span = _number(scenario.get("period_length_hours", minimum), "period_length_hours", positive=True)
    if span < minimum:
        raise ValueError("period_length_hours must cover hours_per_period + overtime_cap_hours")
    return span


def validate_breakdown(scenario: dict, breakdown: dict | None) -> None:
    """Validate an optional outage against the scenario's machine and period IDs."""
    _check_breakdown(normalize(scenario), breakdown)


def apply_breakdown(scenario: dict, breakdown: dict | None) -> dict:
    """Remove an outage from the beginning of a machine's regular window.

    The regular window's end is preserved. Losing all regular availability also
    disables overtime in the planners/replay. Unknown availability cannot be guessed.
    """
    copied = normalize(scenario)
    _check_breakdown(copied, breakdown)
    if breakdown is None:
        return copied
    machine = _target(copied, breakdown["machine"], breakdown["period"])
    period = breakdown["period"]
    available = machine["available"][period]
    if available is None:
        raise ValueError("cannot apply breakdown to missing availability")
    lost = min(float(breakdown["hours"]), available)
    machine["available_start"][period] += lost
    machine["available"][period] = available - lost
    return copied


def missing_availability(scenario: dict) -> list[str]:
    scenario = normalize(scenario)
    missing = []
    for machine in scenario["machines"]:
        for period, hours in zip(scenario["periods"], machine["available"]):
            if hours is None:
                missing.append(f"{machine['id']} {period}")
    return missing


def clone_with_availability(scenario: dict, machine_id: str, period: int, hours: float) -> dict:
    copied = normalize(scenario)
    machine = _target(copied, machine_id, period)
    machine["available"][period] = _number(hours, "replacement availability")
    return normalize(copied)


def fill_missing_with_shift(scenario: dict) -> tuple[dict, list[str]]:
    """Unsafe default used only by the fixed workflow: a blank cell becomes a full shift."""
    copied = normalize(scenario)
    assumed = []
    full = float(scenario["hours_per_period"])
    for machine in copied["machines"]:
        for index, hours in enumerate(machine["available"]):
            if hours is None:
                remaining = max(0.0, full - machine["available_start"][index])
                machine["available"][index] = remaining
                assumed.append(f"{machine['id']} {scenario['periods'][index]} assumed {remaining:g}h")
    return copied, assumed


def _mapping(value, name):
    if not isinstance(value, dict):
        raise ValueError(f"{name} must be an object")
    return value


def _list(value, name, *, nonempty=False):
    if not isinstance(value, list) or (nonempty and not value):
        raise ValueError(f"{name} must be {'a nonempty' if nonempty else 'a'} list")
    return value


def _period_values(value, length, name):
    values = _list(value, name)
    if len(values) != length:
        raise ValueError(f"{name} must have exactly {length} entries")
    return values


def _identifier(value, name):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a nonempty string")
    return value


def _unique_ids(values, name):
    for value in values:
        _identifier(value, name)
    if len(set(values)) != len(values):
        raise ValueError(f"{name} values must be unique")
    return set(values)


def _number(value, name, *, positive=False):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a finite number")
    try:
        result = float(value)
    except (OverflowError, ValueError) as exc:
        raise ValueError(f"{name} must be a finite number") from exc
    if not math.isfinite(result) or result < 0 or (positive and result == 0):
        raise ValueError(f"{name} must be finite and {'positive' if positive else 'nonnegative'}")
    return result


def _integer(value, name):
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{name} must be a nonnegative integer")
    return value


def _target(scenario, machine_id, period):
    _identifier(machine_id, "machine id")
    _integer(period, "period")
    if period >= len(scenario["periods"]):
        raise ValueError("period must be an index within the planning horizon")
    for machine in scenario["machines"]:
        if machine["id"] == machine_id:
            return machine
    raise ValueError(f"unknown machine {machine_id}")


def _check_breakdown(scenario, breakdown):
    if breakdown is None:
        return
    _mapping(breakdown, "breakdown")
    _target(scenario, breakdown.get("machine"), breakdown.get("period"))
    _number(breakdown.get("hours"), "breakdown.hours")
