"""Three-way comparison. The middle arm is there so a better cost is not credited to the agent automatically."""

from __future__ import annotations

import json
from pathlib import Path

from mpa.agent import run as run_agent
from mpa.rules import earliest_due_date
from mpa.scenario import load
from mpa.workflow import run as run_workflow

ROOT = Path(__file__).resolve().parents[1]
SCENARIOS = ("surge_downtime", "missing_downtime", "contradictory_relax")


def evaluate() -> dict:
    report = {"scenarios": []}
    for name in SCENARIOS:
        scenario = load(ROOT / "scenarios" / f"{name}.json")
        rule = earliest_due_date(scenario)
        workflow = run_workflow(scenario)
        agent = run_agent(scenario)
        report["scenarios"].append(
            {
                "name": name,
                "story": scenario["story"],
                "rule": _view(rule),
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
        view["sim_makespan"] = sim.get("makespan")
    return view


def main() -> None:
    report = evaluate()
    out = ROOT / "results" / "comparison.json"
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    for scenario in report["scenarios"]:
        print(scenario["name"])
        for arm in ("rule", "fixed_workflow", "agent"):
            row = scenario[arm]
            print(
                f"  {arm:16} ok={row['ok']} status={row['status']} "
                f"on_time={row['on_time_units']} tardy={row['tardy_units']} "
                f"unfinished={row['unfinished_units']} cost={row['accounting_cost']} "
                f"policy={row.get('policy')}"
            )


if __name__ == "__main__":
    main()
