"""Event-triggered, human-confirmed rolling proposals; no model or live dispatch.

python -m mpa.rolling --output-dir artifacts/rolling
"""
from __future__ import annotations

import argparse
from copy import deepcopy
import hashlib
from html import escape
import json
from pathlib import Path
import subprocess

from mpa.reporting import provenance
from mpa.rolling_solver import solve_remaining
from mpa.rolling_state import event_at, initial_snapshot, key, metrics, resume, validate_snapshot
from mpa.scenario import load

ROOT = Path(__file__).resolve().parents[1]


def snapshot_hash(snapshot):
    return hashlib.sha256(json.dumps(snapshot, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def propose(scenario, snapshot, event, *, policy="cost_min", step=0.5):
    scenario = validate_snapshot(scenario, snapshot)
    if policy not in scenario["policies"]:
        raise ValueError("policy must be on the named menu")
    events = deepcopy(snapshot["events"]) + [event_at(snapshot, event, scenario)]
    baseline = resume(scenario, snapshot, events)
    solved = solve_remaining(scenario, snapshot, events, policy, step)
    report = {"schemaVersion": 1, "currentTime": snapshot["currentTime"], "snapshotHash": snapshot_hash(snapshot),
              "scenarioHash": snapshot["scenarioHash"], "events": events, "snapshot": deepcopy(snapshot),
              "policy": policy, "model_calls": 0, "needs_human_confirm": True,
              "baseline": {"metrics": metrics(scenario, baseline), "state": baseline}, "solver": solved,
              "candidate": None, "recommendation": "keep_original", "reason": "solver did not produce an audited replacement"}
    if solved["ok"]:
        candidate = resume(scenario, snapshot, events, placements=solved["placements"])
        old_score, new_score = score(scenario, baseline, policy), score(scenario, candidate, policy)
        report["baseline"]["policyScore"] = old_score
        report["candidate"] = {"metrics": metrics(scenario, candidate), "state": candidate, "policyScore": new_score}
        if new_score <= old_score + 1e-8:
            report["recommendation"] = "candidate"
            report["reason"] = "audited future proposal is no worse under the selected policy; awaiting human confirmation"
        else:
            report["reason"] = "conservative-grid proposal is worse under the selected policy; retain original routes"
    prefix = snapshot["executedPrefix"]
    for result in [baseline] + ([report["candidate"]["state"]] if report["candidate"] else []):
        if result["executedPrefix"][:len(prefix)] != prefix:
            raise ValueError("execution prefix changed")
        if result["startCommitments"][:len(snapshot["startCommitments"])] != snapshot["startCommitments"]:
            raise ValueError("past start commitments changed")
    return report


def score(scenario, state, policy):
    result = metrics(scenario, state)
    weights = scenario["policies"][policy]
    return (result["tardyUnits"] * weights["tardiness"] + result["unfinishedUnits"] * weights["unfinished"]
            + result["reservedOvertimeHours"] * weights["overtime"])


def advance_confirmed(scenario, snapshot, proposal, until, *, choice="candidate", confirmed=False):
    """Adopt an explicitly confirmed future schedule, then physically advance SimPy.

    A proposal for an older execution state cannot be applied after progress/event
    history changes. Before confirmation, propose() never mutates the execution state.
    """
    if confirmed is not True:
        raise ValueError("explicit human confirmation is required")
    if proposal["snapshotHash"] != snapshot_hash(snapshot):
        raise ValueError("stale proposal: execution snapshot changed")
    if choice not in {"candidate", "original"}:
        raise ValueError("choice must be candidate or original")
    if choice == "candidate" and not proposal["solver"]["ok"]:
        raise ValueError("there is no audited candidate to adopt")
    return resume(scenario, snapshot, proposal["events"],
                  placements=proposal["solver"]["placements"] if choice == "candidate" else None, until=until)


def gantt(scenario, report):
    """Standalone SVG: identical frozen prefix plus before/after future execution."""
    from mpa.rolling_state import horizon
    machines = [m["id"] for m in scenario["machines"]]
    width, left, scale = 1000, 190, 760 / horizon(scenario)
    panels = [("Original routes after the event", report["baseline"]["state"])]
    if report["candidate"]:
        panels.append(("HiGHS future proposal (not yet confirmed)", report["candidate"]["state"]))
    height = 65 + len(panels) * (60 + len(machines) * 36)
    parts = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}" role="img" aria-labelledby="title desc">',
             '<title id="title">Event-triggered rolling schedule comparison</title>',
             '<desc id="desc">Gray is the immutable executed prefix. Purple is future fabrication, green future testing. Red is an outage. Dashed lines mark current time.</desc>',
             '<rect width="100%" height="100%" fill="#fbfaf7"/>',
             '<g font-family="sans-serif" fill="#20211f"><text x="18" y="24" font-size="16">Frozen prefix + resumed work + unstarted routing</text>',
             '<text x="18" y="44" font-size="12">Gray: executed · purple: fab · green: test · red: outage · all times are absolute hours</text>']
    y = 65
    cutoff, prefix_length = report["currentTime"], len(report["snapshot"]["executedPrefix"])
    for title, state in panels:
        parts.append(f'<text x="18" y="{y + 16}" font-size="14">{escape(title)}</text>')
        for row, machine in enumerate(machines):
            top = y + 30 + row * 36
            parts.append(f'<text x="18" y="{top + 19}" font-size="12">{escape(machine)}</text>')
            parts.append(f'<path d="M {left} {top + 28} H 950" stroke="#deddd6"/>')
            for event in report["events"]:
                if event["machine"] == machine:
                    a, b = max(0, event["start"]), min(horizon(scenario), event["end"])
                    parts.append(f'<rect x="{left + a * scale:.3f}" y="{top}" width="{max(0, b - a) * scale:.3f}" height="28" fill="#f5d8d4"/>')
            for index, segment in enumerate(state["executedPrefix"]):
                if segment["machine"] != machine:
                    continue
                color = "#92968e" if index < prefix_length else "#6954d9" if segment["stage"] == "fab" else "#33906d"
                label = f'{segment["order"]}/{segment["unit"]} {segment["stage"]}: {segment["start"]:g}–{segment["end"]:g}'
                parts.append(f'<rect x="{left + segment["start"] * scale:.3f}" y="{top + 3}" width="{(segment["end"] - segment["start"]) * scale:.3f}" height="22" fill="{color}" rx="2"><title>{escape(label)}</title></rect>')
            x = left + cutoff * scale
            parts.append(f'<path d="M {x:.3f} {top} V {top + 28}" stroke="#20211f" stroke-dasharray="4 3"/>')
        axis = y + 35 + len(machines) * 36
        for tick in range(int(horizon(scenario)) + 1):
            parts.append(f'<text x="{left + tick * scale:.3f}" y="{axis}" font-size="10" text-anchor="middle">{tick}</text>')
        y += 60 + len(machines) * 36
    return "".join(parts) + "</g></svg>"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scenario", type=Path, default=ROOT / "scenarios" / "rolling_outage.json")
    parser.add_argument("--output-dir", type=Path, default=ROOT / "results" / "rolling")
    args = parser.parse_args()
    destination = args.output_dir.resolve()
    if destination == ROOT / "results" or (ROOT / "results" / "history") == destination or (ROOT / "results" / "history") in destination.parents:
        raise ValueError("rolling reports must not overwrite the original/history report directories")
    scenario = load(args.scenario)
    config = scenario["rolling"]
    snapshot = initial_snapshot(scenario, config["initialPlan"], config["currentTime"])
    report = propose(scenario, snapshot, config["event"], step=config["gridStep"])
    source = args.scenario.resolve()
    report["provenance"] = provenance([source] if ROOT in source.parents else [])
    report["provenance"]["input"] = {"name": source.name, "sha256": hashlib.sha256(source.read_text(encoding="utf-8").encode()).hexdigest()}
    report["provenance"]["source_commit"] = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True, check=True).stdout.strip()
    report["provenance"]["working_tree_clean"] = not subprocess.run(["git", "status", "--porcelain"], cwd=ROOT, capture_output=True, text=True, check=True).stdout.strip()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    (args.output_dir / "comparison.json").write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    svg = gantt(scenario, report)
    (args.output_dir / "gantt.svg").write_text(svg, encoding="utf-8")
    table = "<table><tr><th>Arm</th><th>On time</th><th>Tardy</th><th>Unfinished</th><th>Reserved OT</th><th>Used OT</th></tr>"
    for name in ("baseline", "candidate"):
        if report[name]:
            m = report[name]["metrics"]
            table += f'<tr><td>{name}</td>' + "".join(f"<td>{m[k]:g}</td>" for k in ("onTimeUnits", "tardyUnits", "unfinishedUnits", "reservedOvertimeHours", "actualOvertimeHours")) + "</tr>"
    html = '<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width"><title>Rolling planning report</title>'
    html += '<style>body{font:16px system-ui;max-width:1100px;margin:30px auto;padding:0 18px;color:#20211f}svg{width:100%;height:auto}.table-scroll{max-width:100%;overflow-x:auto}table{border-collapse:collapse}th,td{padding:10px;border:1px solid #deddd6}code{overflow-wrap:anywhere}</style>'
    html += "<h1>Event-triggered rolling planning</h1><p>Pending human confirmation · deterministic known durations · no model calls · no live dispatch.</p>"
    html += "<p>Both arms start from the same executed prefix and WIP. Started operations keep their original machine and resume after outages/closures without losing processed hours. Only unstarted operations can move. Overtime authorization and due dates stay unchanged.</p>"
    operations = snapshot["operations"]
    html += f'<h2>Execution snapshot at hour {snapshot["currentTime"]:g}</h2><p>'
    counts = [(sum(o["state"] == state for o in operations), "WIP" if state == "wip" else state) for state in ("completed", "wip", "unstarted")]
    html += ", ".join(f'{count} {label} stage{"s" if count != 1 else ""}' for count, label in counts) + ". All demanded fabrication and testing stages remain accounted for.</p>"
    html += '<div class="table-scroll"><table><tr><th>Committed WIP</th><th>Frozen machine</th><th>Processed hours</th><th>Remaining hours</th></tr>'
    for operation in operations:
        if operation["state"] == "wip":
            identity = f'{operation["order"]}/{operation["unit"]} {operation["stage"]}'
            html += f'<tr><td>{escape(identity)}</td><td>{escape(operation["machine"])}</td><td>{operation["processedHours"]:g}</td><td>{operation["remainingHours"]:g}</td></tr>'
    html += '</table></div><h2>Actual execution after the outage</h2><div class="table-scroll">' + table + "</table></div><p>" + escape(report["reason"]) + "</p>" + svg
    html += '<p>Source commit: <code>' + escape(report["provenance"]["source_commit"]) + '</code>. <a href="comparison.json">Snapshot, exact placements, trace and provenance JSON</a></p></html>'
    (args.output_dir / "index.html").write_text(html, encoding="utf-8")
    print(json.dumps({"baseline": report["baseline"]["metrics"], "candidate": report["candidate"]["metrics"] if report["candidate"] else None,
                      "needs_human_confirm": True, "solver": report["solver"]["status"]}))


if __name__ == "__main__":
    main()
