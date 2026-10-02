"""Reports must expose replay failures rather than only feasible MILP totals."""
import hashlib
import json
import subprocess
import sys
from pathlib import Path

from mpa.evaluate import _view
from mpa.reporting import provenance

ROOT = Path(__file__).resolve().parents[1]


def test_view_keeps_unfinished_cost_overtime_and_trace():
    plan = {"ok": True, "status": "optimal", "on_time_units": 1, "unfinished_units": 0}
    replay = {"ok": True, "on_time_units": 0, "tardy_units": 0, "unfinished_units": 1,
              "accounting_cost": 280, "actual_overtime_hours": 0.5, "makespan": 0,
              "execution_segments": [{"start": 7, "end": 8.5}]}
    view = _view(plan, {"simulation": replay, "first_simulation": replay, "initial_plan": plan})
    assert view["unfinished_units"] == 0
    assert view["sim_unfinished_units"] == 1
    assert view["sim_accounting_cost"] == 280
    assert view["sim_actual_overtime_hours"] == 0.5
    assert view["simulation"]["execution_segments"] == replay["execution_segments"]
    assert view["first_simulation"] == replay
    assert view["initial_plan"] == plan


def test_provenance_hashes_actual_sources_and_inputs():
    source = ROOT / "scenarios" / "surge_downtime.json"
    report = provenance([source])
    assert set(report["dependencies"]) == {"numpy", "scipy", "simpy"}
    assert all(report["dependencies"].values())
    assert "scenarios/surge_downtime.json" in report["sha256"]
    assert "mpa/agent.py" in report["sha256"]
    for relative, digest in report["sha256"].items():
        content = (ROOT / relative).read_text(encoding="utf-8").encode("utf-8")
        assert hashlib.sha256(content).hexdigest() == digest


def test_evaluation_cli_preserves_historical_reports(tmp_path):
    protected = [ROOT / "results" / "comparison.json", *sorted((ROOT / "results" / "history").rglob("*.*"))]
    before = {path: path.read_bytes() for path in protected}
    subprocess.run([sys.executable, "-m", "mpa.evaluate", "--output-dir", str(tmp_path / "nested")],
                   cwd=ROOT, check=True, capture_output=True, timeout=90)
    report = json.loads((tmp_path / "nested" / "comparison.json").read_text(encoding="utf-8"))
    assert [row["name"] for row in report["scenarios"]] == ["surge_downtime", "missing_downtime", "contradictory_relax"]
    assert report["provenance"]["sha256"]
    for row in report["scenarios"]:
        for arm in ("rule", "fixed_workflow", "agent"):
            if row[arm]["ok"]:
                assert isinstance(row[arm]["sim_unfinished_units"], int)
                assert row[arm]["sim_accounting_cost"] >= 0
    assert all(path.read_bytes() == raw for path, raw in before.items())
