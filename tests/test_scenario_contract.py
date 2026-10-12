"""Input and calendar contracts; no solver or simulator is needed for these cases."""

from copy import deepcopy

import pytest

import mpa.scenario as scenario_api


def _scenario():
    return {
        "periods": ["D1", "D2"],
        "hours_per_period": 8,
        "overtime_cap_hours": 2,
        "stages": ["fab", "test"],
        "costs": {"overtime_per_hour": 80, "tardiness_per_unit": 50, "unfinished_per_unit": 200},
        "policies": {"cost_min": {"overtime": 80, "tardiness": 50, "unfinished": 200}},
        "machines": [{"id": "M1", "available": [8, 5]}, {"id": "M2", "available": [None, 0]}],
        "orders": [{"id": "A", "qty": 1, "due": 1,
                    "hours": {"fab": {"M1": 1}, "test": {"M2": 1}}}],
    }


def _set(scenario, path, value):
    parent = scenario
    for key in path[:-1]:
        parent = parent[key]
    parent[path[-1]] = value


def test_normalize_returns_independent_validated_copy_with_calendar_defaults():
    source = _scenario()
    before = deepcopy(source)
    normalized = scenario_api.normalize(source)
    assert source == before
    assert normalized["period_length_hours"] == 10
    assert normalized["machines"][0]["available_start"] == [0, 0]
    assert normalized["machines"][1]["available"] == [None, 0]
    assert scenario_api.validate(normalized) is None
    normalized["orders"][0]["qty"] = 2
    assert source["orders"][0]["qty"] == 1


@pytest.mark.parametrize("path", [
    ("hours_per_period",), ("overtime_cap_hours",), ("period_length_hours",),
    ("machines", 0, "available", 0), ("machines", 0, "available_start", 0),
    ("costs", "overtime_per_hour"), ("costs", "tardiness_per_unit"), ("costs", "unfinished_per_unit"),
    ("policies", "cost_min", "overtime"), ("policies", "cost_min", "tardiness"),
    ("policies", "cost_min", "unfinished"), ("orders", 0, "hours", "fab", "M1"),
])
@pytest.mark.parametrize("invalid", [True, "2", -1, float("nan"), float("inf")])
def test_numeric_inputs_reject_booleans_strings_negative_and_nonfinite(path, invalid):
    scenario = _scenario()
    scenario["machines"][0]["available_start"] = [0, 0]
    _set(scenario, path, invalid)
    with pytest.raises(ValueError):
        scenario_api.normalize(scenario)


@pytest.mark.parametrize("path, invalid", [
    (("periods",), []), (("periods",), ["D1", "D1"]), (("periods",), ["D1", ""]),
    (("stages",), ["test", "fab"]), (("stages",), ["fab", "paint"]),
    (("machines", 1, "id"), "M1"), (("machines", 0, "id"), " "),
    (("machines", 0, "available"), [8]), (("machines", 0, "available"), [8, 5, 0]),
    (("machines", 0, "available_start"), [0]), (("machines", 0, "available_start"), None),
    (("machines", 0, "available", 0), 9),
    (("orders", 0, "id"), 1), (("orders", 0, "qty"), True),
    (("orders", 0, "qty"), 1.5), (("orders", 0, "qty"), -1),
    (("orders", 0, "due"), True), (("orders", 0, "due"), 0.0),
    (("orders", 0, "due"), -1), (("orders", 0, "due"), 2),
    (("orders", 0, "hours", "fab", "M1"), 0),
    (("orders", 0, "hours", "test"), {"unknown": 1}),
    (("orders", 0, "hours"), {"fab": {"M1": 1}, "paint": {"M2": 1}}),
    (("sim_seed",), True), (("sim_seed",), 1.5), (("sim_seed",), -1),
    (("policies",), []), (("costs",), {}), (("orders",), []),
])
def test_invalid_structure_and_identifiers_are_rejected(path, invalid):
    scenario = _scenario()
    _set(scenario, path, invalid)
    with pytest.raises(ValueError):
        scenario_api.normalize(scenario)


def test_duplicate_order_ids_are_rejected():
    scenario = _scenario()
    scenario["orders"].append(deepcopy(scenario["orders"][0]))
    with pytest.raises(ValueError, match="id"):
        scenario_api.normalize(scenario)


def test_zero_quantity_cost_and_capacity_are_valid_but_processing_rate_is_positive():
    scenario = _scenario()
    scenario["orders"][0]["qty"] = 0
    scenario["overtime_cap_hours"] = 0
    scenario["policies"]["cost_min"] = dict.fromkeys(("overtime", "tardiness", "unfinished"), 0)
    assert scenario_api.normalize(scenario)["orders"][0]["qty"] == 0


def test_regular_windows_fit_shift_and_periods_do_not_overlap():
    scenario = _scenario()
    scenario["machines"][0]["available_start"] = [0, 3]
    scenario["period_length_hours"] = 24
    assert scenario_api.period_length(scenario) == 24
    assert scenario_api.normalize(scenario)["machines"][0]["available_start"] == [0, 3]
    scenario["machines"][0]["available_start"][1] = 4
    with pytest.raises(ValueError):
        scenario_api.normalize(scenario)
    scenario["machines"][0]["available_start"][1] = 3
    scenario["period_length_hours"] = 9
    with pytest.raises(ValueError):
        scenario_api.normalize(scenario)


@pytest.mark.parametrize("breakdown", [
    {}, {"machine": "unknown", "period": 0, "hours": 2},
    {"machine": "M1", "period": -1, "hours": 2},
    {"machine": "M1", "period": 2, "hours": 2},
    {"machine": "M1", "period": True, "hours": 2},
    {"machine": "M1", "period": 0.0, "hours": 2},
    {"machine": "M1", "period": 0, "hours": True},
    {"machine": "M1", "period": 0, "hours": -1},
    {"machine": "M1", "period": 0, "hours": float("nan")},
    {"machine": "M1", "period": 0, "hours": float("inf")},
])
def test_invalid_breakdown_rejected_both_as_metadata_and_as_operation(breakdown):
    scenario = _scenario()
    scenario["sim_breakdown"] = breakdown
    with pytest.raises(ValueError):
        scenario_api.normalize(scenario)
    del scenario["sim_breakdown"]
    with pytest.raises(ValueError):
        scenario_api.apply_breakdown(scenario, breakdown)


@pytest.mark.parametrize("hours, expected_start, expected_available", [(0, 1, 5), (2, 3, 3), (5, 6, 0), (99, 6, 0)])
def test_breakdown_removes_beginning_of_existing_window_without_moving_end(hours, expected_start, expected_available):
    scenario = _scenario()
    scenario["machines"][0]["available_start"] = [0, 1]
    before = deepcopy(scenario)
    updated = scenario_api.apply_breakdown(scenario, {"machine": "M1", "period": 1, "hours": hours})
    assert scenario == before
    assert updated["machines"][0]["available_start"] == [0, expected_start]
    assert updated["machines"][0]["available"] == [8, expected_available]
    assert expected_start + expected_available == 6
    assert updated["machines"][1]["available"] == [None, 0]


def test_breakdown_does_not_guess_unknown_availability():
    scenario = _scenario()
    with pytest.raises(ValueError, match="missing"):
        scenario_api.apply_breakdown(scenario, {"machine": "M2", "period": 0, "hours": 2})
    assert scenario_api.apply_breakdown(scenario, None)["machines"][1]["available"][0] is None


@pytest.mark.parametrize("machine, period, hours", [
    ("unknown", 0, 1), ("M1", -1, 1), ("M1", 2, 1), ("M1", True, 1),
    ("M1", 0, True), ("M1", 0, "2"), ("M1", 0, -1),
    ("M1", 0, float("nan")), ("M1", 0, 9),
])
def test_availability_replacement_rejects_invalid_target_or_value(machine, period, hours):
    with pytest.raises(ValueError):
        scenario_api.clone_with_availability(_scenario(), machine, period, hours)


def test_availability_replacement_and_explicit_unsafe_fill_remain_compatible():
    scenario = _scenario()
    updated = scenario_api.clone_with_availability(scenario, "M2", 0, 3)
    assert updated["machines"][1]["available"] == [3, 0]
    assert scenario["machines"][1]["available"] == [None, 0]
    scenario["machines"][1]["available_start"] = [2, 0]
    filled, assumed = scenario_api.fill_missing_with_shift(scenario)
    assert filled["machines"][1]["available"] == [6, 0]
    assert assumed == ["M2 D1 assumed 6h"]
    assert scenario["machines"][1]["available"][0] is None
