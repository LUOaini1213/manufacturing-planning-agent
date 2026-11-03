"""Robustness sweep for the three-arm comparison around the Agent's policy threshold.

The published surge_downtime row sits exactly on the Agent's hand-written switch
(8 of 32 fab hours lost = 0.25). This sweep varies M2's D2 availability over
0..8 hours and the replay seed over 1..20 (180 scenarios) and records, for every
scenario, the replay of the rule, the fixed workflow, the Agent, and the same
Agent with its policy pinned to `cost_min` or to `due_first`.

    python -m mpa.sweep                 # rewrite results/sweep.json
    python -m mpa.sweep --check         # regenerate and compare with results/sweep.json
"""

from __future__ import annotations

import argparse
import copy
import json
import sys
from pathlib import Path

from mpa.agent import DUE_FIRST_LOSS_FRACTION, run as run_agent
from mpa.reporting import provenance
from mpa.rules import earliest_due_date
from mpa.scenario import load
from mpa.simulate import simulate
from mpa.workflow import run as run_workflow

ROOT = Path(__file__).resolve().parents[1]
SCENARIO = ROOT / "scenarios" / "surge_downtime.json"
MACHINE = "M2"
PERIOD = 1
HOURS = tuple(range(0, 9))
SEEDS = tuple(range(1, 21))
ARMS = ("rule", "fixed_workflow", "agent")
FORCED = ("agent_forced_cost_min", "agent_forced_due_first")


def variant(base: dict, hours: float, seed: int) -> dict:
    scenario = copy.deepcopy(base)
    machine = next(item for item in scenario["machines"] if item["id"] == MACHINE)
    machine["available"][PERIOD] = hours
    scenario["sim_seed"] = seed
    return scenario


def _replay(sim: dict | None) -> dict | None:
    if not sim:
        return None
    return {
        "on_time": sim["on_time_units"],
        "tardy": sim["tardy_units"],
        "unfinished": sim["unfinished_units"],
        "replay_cost": round(float(sim["accounting_cost"]), 6),
    }


def run_one(base: dict, hours: float, seed: int) -> dict:
    scenario = variant(base, hours, seed)
    rule = earliest_due_date(scenario)
    rule_sim = simulate(scenario, rule, scenario.get("sim_breakdown")) if rule.get("ok") else None
    workflow = run_workflow(scenario)
    agent = run_agent(scenario)
    # Threshold 0 always satisfies `fraction >= threshold`; an unreachable one never does.
    forced_due = run_agent(scenario, due_first_threshold=0.0)
    forced_cost = run_agent(scenario, due_first_threshold=2.0)
    choice = next(item for item in agent["log"] if item["tool"] == "select_policy")
    return {
        "m2_d2_hours": hours,
        "seed": seed,
        "fab_hours_lost_fraction": choice["fab_hours_lost_fraction"],
        "agent_policy": choice["choice"],
        "rule": _replay(rule_sim),
        "fixed_workflow": _replay(workflow["simulation"]),
        "agent": _replay(agent["simulation"]),
        "agent_forced_cost_min": _replay(forced_cost["simulation"]),
        "agent_forced_due_first": _replay(forced_due["simulation"]),
    }


def _compare(a: float, b: float) -> str:
    if a < b - 1e-6:
        return "better"
    if a > b + 1e-6:
        return "worse"
    return "equal"


def summarize(rows: list[dict]) -> dict:
    groups: dict = {}
    for row in rows:
        groups.setdefault(row["m2_d2_hours"], []).append(row)
    by_hours = []
    for hours in sorted(groups):
        group = groups[hours]
        by_hours.append(_summary(group, {"m2_d2_hours": hours,
                                         "fab_hours_lost_fraction": group[0]["fab_hours_lost_fraction"]}))
    return {"by_m2_d2_hours": by_hours, "overall": _summary(rows, {})}


def _summary(rows: list[dict], head: dict) -> dict:
    out = dict(head)
    out["scenarios"] = len(rows)
    out["agent_policy"] = {policy: sum(row["agent_policy"] == policy for row in rows)
                           for policy in sorted({row["agent_policy"] for row in rows})}
    lowest = {arm: 0 for arm in ARMS}
    for row in rows:
        best = min(row[arm]["replay_cost"] for arm in ARMS)
        for arm in ARMS:
            if row[arm]["replay_cost"] <= best + 1e-6:
                lowest[arm] += 1
    out["lowest_replay_cost_including_ties"] = lowest
    out["agent_vs_fixed_workflow"] = _tally(rows, "agent", "fixed_workflow")
    out["forced_due_first_vs_forced_cost_min"] = _tally(rows, "agent_forced_due_first", "agent_forced_cost_min")
    out["mean_replay_cost"] = {arm: round(sum(row[arm]["replay_cost"] for row in rows) / len(rows), 2)
                               for arm in ARMS + FORCED}
    return out


def _tally(rows: list[dict], left: str, right: str) -> dict:
    tally = {"better": 0, "equal": 0, "worse": 0}
    for row in rows:
        tally[_compare(row[left]["replay_cost"], row[right]["replay_cost"])] += 1
    return tally


def sweep(hours=HOURS, seeds=SEEDS) -> dict:
    base = load(SCENARIO)
    rows = [run_one(base, h, seed) for h in hours for seed in seeds]
    return {
        "config": {
            "scenario": SCENARIO.relative_to(ROOT).as_posix(),
            "varied": f"{MACHINE} availability in period {PERIOD} (D2), hours",
            "m2_d2_hours": list(hours),
            "seeds": list(seeds),
            "due_first_loss_fraction": DUE_FIRST_LOSS_FRACTION,
            "replay_cost": "reserved overtime x 80 + tardy x 50 + unfinished x 200, after the seeded replay "
                           "with the scenario's M1 D2 3-hour breakdown",
            "forced_arms": "same Agent code with due_first_threshold=2.0 (never due_first) or 0.0 (always due_first)",
        },
        "rows": rows,
        "summary": summarize(rows),
    }


def _tally_cell(tally: dict) -> str:
    return f"{tally['better']} / {tally['equal']} / {tally['worse']}"


def _num(value: float) -> str:
    return f"{value:g}"


def readme_table(summary: dict) -> str:
    """The README table, rendered from the committed summary so tests can pin it verbatim."""
    lines = [
        "| M2 D2 可用工时 | 加工损失比例 | Agent 策略 | Agent 对固定流程 更低 / 相同 / 更高 | 平均回放费用 规则 / 固定流程 / Agent | 强制 `due_first` 对强制 `cost_min` 更低 / 相同 / 更高 |",
        "|---:|---:|---|---:|---:|---:|",
    ]
    for row in summary["by_m2_d2_hours"]:
        policies = ", ".join(f"{name} {count}" for name, count in row["agent_policy"].items())
        cost = row["mean_replay_cost"]
        lines.append(
            f"| {_num(row['m2_d2_hours'])} | {row['fab_hours_lost_fraction']:.4f} | {policies} | "
            f"{_tally_cell(row['agent_vs_fixed_workflow'])} | "
            f"{_num(cost['rule'])} / {_num(cost['fixed_workflow'])} / {_num(cost['agent'])} | "
            f"{_tally_cell(row['forced_due_first_vs_forced_cost_min'])} |"
        )
    return "\n".join(lines)


def comparable(report: dict) -> dict:
    return {key: report[key] for key in ("config", "rows", "summary")}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--output-dir", type=Path, default=ROOT / "results")
    parser.add_argument("--check", action="store_true", help="regenerate and compare with the committed results/sweep.json")
    args = parser.parse_args(argv)
    report = sweep()
    if args.check:
        committed = json.loads((ROOT / "results" / "sweep.json").read_text(encoding="utf-8"))
        fresh = json.loads(json.dumps(comparable(report)))
        if fresh != comparable(committed):
            for key in ("config", "summary", "rows"):
                if fresh[key] != committed[key]:
                    print(f"results/sweep.json is stale: '{key}' differs from a fresh run", file=sys.stderr)
            return 1
        print(f"results/sweep.json matches a fresh run ({len(report['rows'])} scenarios)")
        return 0
    report["provenance"] = provenance([SCENARIO])
    out = args.output_dir / "sweep.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    for row in report["summary"]["by_m2_d2_hours"]:
        print(json.dumps(row, ensure_ascii=False))
    print(json.dumps(report["summary"]["overall"], ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
