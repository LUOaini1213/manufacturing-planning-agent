"""Load a planning scenario. Null availability is missing data, not zero."""

from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path


def load(path: str | Path) -> dict:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    return normalize(data)


def normalize(data: dict) -> dict:
    scenario = deepcopy(data)
    scenario.setdefault("request", "")
    scenario.setdefault("sim_breakdown", None)
    scenario.setdefault("sim_seed", 0)
    for machine in scenario["machines"]:
        machine["available"] = [None if hours is None else float(hours) for hours in machine["available"]]
    return scenario


def missing_availability(scenario: dict) -> list[str]:
    missing = []
    for machine in scenario["machines"]:
        for period, hours in zip(scenario["periods"], machine["available"]):
            if hours is None:
                missing.append(f"{machine['id']} {period}")
    return missing


def clone_with_availability(scenario: dict, machine_id: str, period: int, hours: float) -> dict:
    copied = deepcopy(scenario)
    for machine in copied["machines"]:
        if machine["id"] == machine_id:
            machine["available"][period] = float(hours)
    return copied


def fill_missing_with_shift(scenario: dict) -> tuple[dict, list[str]]:
    """Unsafe default used only by the fixed workflow: a blank cell becomes a full shift."""
    copied = deepcopy(scenario)
    assumed = []
    full = float(scenario["hours_per_period"])
    for machine in copied["machines"]:
        for index, hours in enumerate(machine["available"]):
            if hours is None:
                machine["available"][index] = full
                assumed.append(f"{machine['id']} {scenario['periods'][index]} assumed {full:g}h")
    return copied, assumed
