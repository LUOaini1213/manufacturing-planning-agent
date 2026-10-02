"""Three-way comparison. The middle arm is there so a better cost is not credited to the agent automatically."""

from __future__ import annotations

import json
import argparse
from pathlib import Path

from mpa.agent import run as run_agent
from mpa.rules import earliest_due_date
from mpa.scenario import load
from mpa.workflow import run as run_workflow
from mpa.simulate import simulate
from mpa.reporting import provenance

ROOT = Path(__file__).resolve().parents[1]
SCENARIOS = ("surge_downtime", "missing_downtime", "contradictory_relax")


def evaluate() -> dict:
    report = {"scenarios": []}
    for name in SCENARIOS:
        scenario = load(ROOT / "scenarios" / f"{name}.json")
        rule = earliest_due_date(scenario)
        rule_sim = simulate(scenario, rule, scenario.get("sim_breakdown")) if rule.get("ok") else None
        workflow = run_workflow(scenario)
        agent = run_agent(scenario)
        report["scenarios"].append(
            {
                "name": name,
                "story": scenario["story"],
                "rule": _view(rule, {"simulation": rule_sim}),
                "fixed_workflow": _view(workflow["plan"], workflow),
                "agent": _view(agent["plan"], agent),
            }
        )
    return report


def _view(plan: dict, wrapper: dict | None = None) -> dict:
    view = {
        "ok": bool(plan.get("ok")),
        "status": plan.get("status"),
        "policy": plan.get("policy"),
        "on_time_units": plan.get("on_time_units"),
        "tardy_units": plan.get("tardy_units"),
        "unfinished_units": plan.get("unfinished_units"),
        "overtime_hours": plan.get("overtime_hours"),
        "accounting_cost": plan.get("accounting_cost"),
        "reason": plan.get("reason"),
    }
    if wrapper:
        view["note"] = wrapper.get("note")
        view["model_calls"] = wrapper.get("model_calls", 0)
        view["needs_human_confirm"] = wrapper.get("needs_human_confirm")
        view["log"] = wrapper.get("log")
        sim = wrapper.get("simulation") or {}
        view["sim_on_time_units"] = sim.get("on_time_units")
        view["sim_tardy_units"] = sim.get("tardy_units")
        view["sim_unfinished_units"] = sim.get("unfinished_units")
        view["sim_accounting_cost"] = sim.get("accounting_cost")
        view["sim_actual_overtime_hours"] = sim.get("actual_overtime_hours")
        view["sim_makespan"] = sim.get("makespan")
        view["simulation"] = wrapper.get("simulation")
        view["first_simulation"] = wrapper.get("first_simulation")
        view["initial_plan"] = wrapper.get("initial_plan")
    view["plan"] = plan
    return view


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=ROOT / "results")
    args = parser.parse_args()
    report = evaluate()
    report["provenance"] = provenance([ROOT / "scenarios" / f"{name}.json" for name in SCENARIOS])
    out = args.output_dir / "comparison.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    for scenario in report["scenarios"]:
        print(scenario["name"])
        for arm in ("rule", "fixed_workflow", "agent"):
            row = scenario[arm]
            print(
                f"  {arm:16} allocation: ok={row['ok']} status={row['status']} "
                f"on_time={row['on_time_units']} tardy={row['tardy_units']} "
                f"unfinished={row['unfinished_units']} cost={row['accounting_cost']} "
                f"policy={row.get('policy')}"
            )
            if row.get("simulation"):
                print(f"    replay: on_time={row['sim_on_time_units']} tardy={row['sim_tardy_units']} "
                      f"unfinished={row['sim_unfinished_units']} reserved_cost={row['sim_accounting_cost']} "
                      f"actual_overtime={row['sim_actual_overtime_hours']:.4f}h")


if __name__ == "__main__":
    main()
