"""Independent CLSP checks, including plans the optimizer would never generate."""

from copy import deepcopy
import json
import sys
from types import SimpleNamespace

import numpy as np
import pytest

from mpa import clsp


def instance(**changes):
    data = {
        "n_items": 1,
        "n_periods": 2,
        "capacity": 2,
        "unit_time": [1],
        "holding": [1],
        "setup_time": [0],
        "setup_cost": [1],
        "demand": [[0], [1]],
    }
    data.update(changes)
    return data


@pytest.mark.parametrize("production", [
    [[2, -1]], [[1, -1e-15]], [[True, 1]], [[False, 1]],
    [[float("nan"), 1]], [[float("inf"), 1]], [[0, float("-inf")]],
    [["0", 1]],
])
def test_audit_rejects_invalid_quantities(production):
    with pytest.raises(ValueError, match="production"):
        clsp.audit(instance(), production)


@pytest.mark.parametrize("production", [
    [], [[0]], [[0, 1, 0]], [[0, 1], [0, 0]], [0], None,
])
def test_audit_requires_exact_production_shape(production):
    with pytest.raises(ValueError, match="production"):
        clsp.audit(instance(), production)


@pytest.mark.parametrize("field,value", [
    ("n_items", True), ("n_items", 1.5), ("n_items", 0),
    ("n_periods", 0), ("n_periods", float("inf")),
    ("capacity", -1), ("capacity", True), ("capacity", float("nan")),
    ("unit_time", [float("inf")]), ("holding", [-1]),
    ("setup_time", [False]), ("setup_cost", [float("nan")]),
    ("demand", [[0], [-1]]), ("demand", [[0], [True]]),
    ("demand", [[0], [float("nan")]]),
    ("unit_time", []), ("holding", [1, 2]), ("setup_time", [[0]]),
    ("setup_cost", None), ("demand", [[0]]),
    ("demand", [[0], [1], [0]]), ("demand", [[0, 2], [1]]),
])
@pytest.mark.parametrize("entrypoint", ["audit", "solve_clsp"])
def test_invalid_model_rejected_before_computation(field, value, entrypoint, monkeypatch):
    def unexpected_solver(*args, **kwargs):
        pytest.fail("invalid model reached the optimizer")

    monkeypatch.setattr(clsp, "milp", unexpected_solver)
    data = instance(**{field: value})
    with pytest.raises(ValueError, match=field):
        if entrypoint == "audit":
            clsp.audit(data, [[0, 1]])
        else:
            clsp.solve_clsp(data)


@pytest.mark.parametrize("entrypoint", ["audit", "solve_clsp"])
def test_missing_model_field_is_a_clear_value_error(entrypoint):
    data = instance()
    del data["holding"]
    with pytest.raises(ValueError, match="holding"):
        if entrypoint == "audit":
            clsp.audit(data, [[0, 1]])
        else:
            clsp.solve_clsp(data)


def test_tiny_positive_lot_still_requires_setup_capacity():
    data = instance(n_periods=1, capacity=1, setup_time=[2], demand=[[1e-9]])
    with pytest.raises(ValueError, match="period 0.*load"):
        clsp.audit(data, [[1e-9]])


def test_tiny_positive_lot_still_pays_setup_cost():
    data = instance(n_periods=1, capacity=3, setup_time=[2], setup_cost=[7], demand=[[1e-9]])
    checked = clsp.audit(data, [[1e-9]])
    assert checked["objective"] == 7
    assert checked["loads"][0] == pytest.approx(2 + 1e-9)


def test_actual_backlog_and_overload_are_not_hidden_by_decimal_tolerance():
    data = instance(n_periods=1, capacity=1, demand=[[1]])
    with pytest.raises(ValueError, match="short"):
        clsp.audit(data, [[1 - 2e-5]])
    with pytest.raises(ValueError, match="load"):
        clsp.audit(data, [[1 + 2e-5]])


def test_shortages_do_not_reset_to_zero_at_each_period():
    data = instance(n_periods=4, demand=[[6e-5]] * 4)
    with pytest.raises(ValueError, match="short"):
        clsp.audit(data, [[0] * 4])


def test_ieee_roundoff_is_allowed_without_mutating_fractional_input():
    data = instance(n_periods=1, capacity=0.3, demand=[[0.3]])
    production = [[0.1 + 0.2]]
    saved = deepcopy((data, production))
    assert clsp.audit(data, production)["objective"] == 1
    assert (data, production) == saved
    data["demand"] = [[0.1 + 0.2]]
    assert clsp.audit(data, [[0.3]])["objective"] == 1


def test_small_continuous_optimum_is_independently_computable():
    data = instance(demand=[[0.25], [0.375]], setup_time=[0.125], setup_cost=[3], holding=[2])
    optimal = clsp.solve_clsp(data)
    assert optimal["ok"]
    # One setup plus 0.375 units held one period: 3 + 2 * 0.375.
    assert optimal["objective"] == 3.75
    assert optimal["production"][0] == pytest.approx([0.625, 0])
    assert clsp.audit(data, optimal["production"])["objective"] == 3.75
    assert optimal["setups"] == 1


def test_solver_keeps_continuous_quantities_beyond_four_decimals():
    data = instance(n_periods=1, demand=[[0.123456789]], unit_time=[2], setup_time=[0.1])
    optimal = clsp.solve_clsp(data)
    assert optimal["ok"]
    assert optimal["production"][0][0] == pytest.approx(0.123456789, abs=1e-12, rel=0)
    assert clsp.audit(data, optimal["production"])["objective"] == optimal["objective"]


def test_solver_result_is_independently_audited(monkeypatch):
    # Correct vector shape but an impossible negative production quantity.
    fake = SimpleNamespace(success=True, status=0, message="success", fun=3,
                           x=np.array([2., -1., 2., 0., 1., 0.]), mip_gap=0)
    monkeypatch.setattr(clsp, "milp", lambda *args, **kwargs: fake)
    result = clsp.solve_clsp(instance())
    assert result["ok"] is False
    assert "production" in result["reason"]


def test_infeasible_capacity_is_a_solver_result_not_a_schema_error():
    data = instance(n_periods=1, capacity=0, demand=[[1]])
    result = clsp.solve_clsp(data)
    assert result["ok"] is False


@pytest.mark.parametrize("noise", [-1e-14, 1e-14])
def test_solver_zero_setup_roundoff_is_normalized_only_at_solver_boundary(monkeypatch, noise):
    fake = SimpleNamespace(success=True, status=0, message="success", fun=1,
                           x=np.array([noise, 1., 0., 0., 0., 1.]), mip_gap=0)
    monkeypatch.setattr(clsp, "milp", lambda *args, **kwargs: fake)
    result = clsp.solve_clsp(instance())
    assert result["ok"]
    assert result["production"] == [[0, 1]]
    if noise < 0:
        with pytest.raises(ValueError, match="production"):
            clsp.audit(instance(), [[noise, 1]])
    else:
        assert clsp.audit(instance(), [[noise, 1]])["objective"] == 2


def test_solver_must_not_treat_a_real_positive_lot_as_zero_setup_roundoff(monkeypatch):
    fake = SimpleNamespace(success=True, status=0, message="success", fun=1,
                           x=np.array([1e-12, 1., 0., 0., 0., 1.]), mip_gap=0)
    monkeypatch.setattr(clsp, "milp", lambda *args, **kwargs: fake)
    result = clsp.solve_clsp(instance())
    assert not result["ok"]
    assert "objective" in result["reason"]


@pytest.mark.parametrize("quantity,setup", [
    (-1.7337242752546445e-12, -2.842170943040401e-14),
    (-6.801323817555763e-12, -8.194366045247908e-14),
])
def test_exact_gap_solver_signed_zero_residuals_still_require_audited_feasibility(monkeypatch, quantity, setup):
    # Observed F1 residuals with mip_rel_gap=0, well inside the quantity ULP bound.
    data = instance(capacity=2000, demand=[[0], [1260]])
    fake = SimpleNamespace(success=True, status=0, message="success", fun=1,
                           x=np.array([quantity, 1260., 0., 0., setup, 1.]), mip_gap=0)
    monkeypatch.setattr(clsp, "milp", lambda *args, **kwargs: fake)
    result = clsp.solve_clsp(data)
    assert result["ok"]
    assert result["mip_gap"] == 0
    assert result["production"] == [[0, 1260]]
    assert clsp.audit(data, result["production"])["objective"] == 1
    with pytest.raises(ValueError, match="production"):
        clsp.audit(data, [[quantity, 1260]])


def test_solver_does_not_remove_a_tiny_lot_with_an_active_setup(monkeypatch):
    fake = SimpleNamespace(success=True, status=0, message="success", fun=2,
                           x=np.array([1e-14, 1., 1e-14, 1e-14, 1., 1.]), mip_gap=0)
    monkeypatch.setattr(clsp, "milp", lambda *args, **kwargs: fake)
    result = clsp.solve_clsp(instance())
    assert result["ok"]
    assert result["production"][0][0] == 1e-14
    assert result["setups"] == 2


@pytest.mark.parametrize("tolerance", [True, -1, float("nan"), float("inf"), 0.5])
def test_solver_binary_zero_tolerance_must_be_finite_and_unambiguous(tolerance):
    with pytest.raises(ValueError, match="binary_zero_tolerance"):
        clsp.solve_clsp(instance(), binary_zero_tolerance=tolerance)


@pytest.mark.parametrize("entrypoint", ["lot_for_lot", "shift_earlier", "merge_earlier", "coordinate_plan"])
@pytest.mark.parametrize("field,value", [
    ("capacity", float("nan")), ("demand", [[0], [-1]]),
    ("setup_cost", [True]), ("holding", [1, 2]),
])
def test_heuristic_public_entries_validate_the_model(entrypoint, field, value):
    with pytest.raises(ValueError, match=field):
        getattr(clsp, entrypoint)(instance(**{field: value}))


@pytest.mark.parametrize("entrypoint", ["lot_for_lot", "shift_earlier", "merge_earlier"])
@pytest.mark.parametrize("quantity", [1e-12, 0.375])
def test_continuous_heuristics_account_for_every_positive_lot(entrypoint, quantity):
    data = instance(n_periods=1, demand=[[quantity]], capacity=3, setup_time=[2], setup_cost=[7])
    result = getattr(clsp, entrypoint)(data)
    assert result["ok"]
    assert result["production"] == [[quantity]]
    assert result["setups"] == 1
    assert result["objective"] == clsp.audit(data, result["production"])["objective"] == 7


@pytest.mark.parametrize("quantity", [0.375, 1e-12])
def test_coordinate_plan_rejects_noninteger_demand_without_rounding_it_away(quantity):
    with pytest.raises(ValueError, match="integer demand"):
        clsp.coordinate_plan(instance(n_periods=1, demand=[[quantity]]))


def test_zero_setup_time_can_still_shift_production_out_of_an_overloaded_period():
    data = instance(n_items=2, demand=[[1, 0], [1, 2]], unit_time=[1, 1],
                    holding=[1, 1], setup_time=[0, 0], setup_cost=[0, 0])
    result = clsp.merge_earlier(data)
    assert result["ok"]
    assert result["production"] == [[2, 0], [0, 2]]
    assert clsp.audit(data, result["production"])["objective"] == result["objective"] == 1


def test_coordinate_zero_unit_time_keeps_an_audited_feasible_plan():
    data = instance(demand=[[1], [1]], unit_time=[0])
    result = clsp.coordinate_plan(data)
    assert result["ok"]
    assert clsp.audit(data, result["production"])["objective"] == result["objective"]


def test_clsp_cli_can_write_to_an_explicit_directory_with_provenance(tmp_path, monkeypatch, capsys):
    output = tmp_path / "reports" / "nested"
    monkeypatch.setattr(sys, "argv", ["mpa.clsp", "--output-dir", str(output)])
    monkeypatch.setattr(clsp, "load_trigeiro", instance)
    for name in ("solve_clsp", "lot_for_lot", "shift_earlier", "merge_earlier", "coordinate_plan"):
        monkeypatch.setattr(clsp, name, lambda data: {"ok": True, "objective": 1, "setups": 1})

    def provenance(paths):
        assert paths == [clsp.F1]
        return {"verified_input": "F1.DAT"}

    monkeypatch.setattr(clsp, "provenance", provenance)
    clsp.main()
    saved = json.loads((output / "trigeiro_f1.json").read_text(encoding="utf-8"))
    assert saved["provenance"] == {"verified_input": "F1.DAT"}
    assert json.loads(capsys.readouterr().out) == saved


VALID_DAT = "1 2\n1\n2\n1 1 0 1\n0\n1\n"


@pytest.mark.parametrize("text", [
    "", "1.5 2\n1\n2\n1 1 0 1\n0\n1\n",
    "nan 2\n1\n2\n1 1 0 1\n0\n1\n",
    VALID_DAT.replace("1 2\n", "1 2 3\n", 1),
    VALID_DAT.replace("\n2\n", "\n2 9\n", 1),
    VALID_DAT.replace("1 1 0 1", "1 1 0"),
    VALID_DAT.replace("1 1 0 1", "1 1 0 1 9"),
    VALID_DAT.replace("1 1 0 1", "1 -1 0 1"),
    VALID_DAT.replace("\n0\n", "\n0 1\n"),
    VALID_DAT.replace("\n0\n", "\nnan\n"),
    VALID_DAT + "99\n",
])
def test_trigeiro_rejects_malformed_numeric_records(tmp_path, text):
    path = tmp_path / "bad.DAT"
    path.write_text(text, encoding="utf-8")
    with pytest.raises(ValueError):
        clsp.load_trigeiro(path)


def test_trigeiro_accepts_fractional_data_and_a_text_footer(tmp_path):
    path = tmp_path / "fractional.DAT"
    path.write_text(VALID_DAT.replace("\n0\n1\n", "\n0.25\n0.375\n") + "column headings\n", encoding="utf-8")
    data = clsp.load_trigeiro(path)
    assert data["demand"] == [[0.25], [0.375]]
    assert clsp.solve_clsp(data)["ok"]
