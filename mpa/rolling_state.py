"""Actual SimPy cut/resume state for the deterministic two-stage rolling mode.

Started operations retain their machine and FIFO ownership across closures/outages;
processing pauses and resumes without scrap or restart. Only unstarted operations
may be rerouted/resequenced. This is an explicit resumable-work assumption, not a
claim about non-resumable manufacturing processes or live machine integration.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import math

import simpy

from mpa.scenario import normalize, period_length
from mpa.simulate import _number, _pair_jobs, _validate_plan

EPS = 1e-8


def scenario_hash(scenario):
    return hashlib.sha256(json.dumps(normalize(scenario), sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def key(row):
    if not isinstance(row, dict) or not isinstance(row.get("order"), str) or not isinstance(row.get("stage"), str) or isinstance(row.get("unit"), bool) or not isinstance(row.get("unit"), int):
        raise ValueError("operation identity requires order/stage strings and an integer unit")
    return row["order"], row["unit"], row["stage"]


def horizon(scenario):
    return len(scenario["periods"]) * period_length(scenario)


def calendars(scenario, overtime, events):
    """Subtract explicit absolute-clock outages without moving regular window ends."""
    span = period_length(scenario)
    authorized = {(r["machine"], r["period"]): r["hours"] for r in overtime}
    result = {}
    for machine in scenario["machines"]:
        windows = []
        for period, hours in enumerate(machine["available"]):
            if hours is None:
                raise ValueError("rolling mode cannot guess missing availability")
            start = period * span + machine["available_start"][period]
            regular_end = start + hours
            pieces = [(start, regular_end + authorized.get((machine["id"], period), 0))] if hours > 0 else []
            for event in events:
                if event["machine"] != machine["id"]:
                    continue
                next_pieces = []
                for a, b in pieces:
                    if event["end"] <= a or event["start"] >= b:
                        next_pieces.append((a, b))
                    else:
                        if a < event["start"]:
                            next_pieces.append((a, event["start"]))
                        if event["end"] < b:
                            next_pieces.append((event["end"], b))
                pieces = next_pieces
            windows.extend({"start": a, "end": b, "period": period, "regularEnd": regular_end}
                           for a, b in pieces if b - a > EPS)
        result[machine["id"]] = windows
    return result


def event_at(snapshot, event, scenario):
    if not isinstance(event, dict) or set(event) != {"machine", "start", "end"}:
        raise ValueError("event must specify only machine/start/end")
    if event["machine"] not in {m["id"] for m in scenario["machines"]}:
        raise ValueError("event has unknown machine")
    start, end = _number(event["start"], "event start"), _number(event["end"], "event end")
    if start < snapshot["currentTime"] - EPS or end <= start or start > horizon(scenario):
        raise ValueError("event must affect the future, never the executed prefix")
    return {"machine": event["machine"], "start": start, "end": end}


def work(windows, start, hours):
    """Earliest resumable segments and unprocessed remainder, with no fabricated capacity."""
    remaining, segments = hours, []
    for window in windows:
        a = max(start, window["start"])
        duration = min(remaining, max(0, window["end"] - a))
        if duration > EPS:
            segments.append({"start": a, "end": a + duration, "period": window["period"],
                             "overtimeHours": max(0, a + duration - max(a, window["regularEnd"]))})
            remaining = max(0, remaining - duration)
        if remaining <= EPS:
            break
    return segments, remaining


def initial_snapshot(scenario, plan, current_time):
    scenario = normalize(scenario)
    current_time = _number(current_time, "currentTime")
    if current_time > horizon(scenario):
        raise ValueError("currentTime exceeds the horizon")
    _validate_plan(scenario, plan)
    routes = {key(job): job for pair in _pair_jobs(plan["assignment"]) for job in pair if job is not None}
    operations = []
    for order in scenario["orders"]:
        for unit in range(order["qty"]):
            for stage in scenario["stages"]:
                route = routes.get((order["id"], unit, stage))
                duration = route["hours_each"] if route else None
                operations.append({"order": order["id"], "unit": unit, "stage": stage, "state": "unstarted",
                    "machine": route["machine"] if route else None, "plannedPeriod": route["period"] if route else None,
                    "plannedStart": route["period"] * period_length(scenario) if route else None,
                    "requiredHours": duration, "processedHours": 0.0, "remainingHours": duration,
                    "startedAt": None, "completedAt": None})
    empty = {"schemaVersion": 1, "scenarioHash": scenario_hash(scenario), "currentTime": 0.0,
             "overtime": deepcopy(plan.get("overtime", [])), "events": [], "operations": operations,
             "executedPrefix": [], "startCommitments": []}
    return resume(scenario, empty, [], until=current_time)


def validate_snapshot(scenario, snapshot):
    scenario = normalize(scenario)
    if not isinstance(snapshot, dict) or snapshot.get("schemaVersion") != 1 or snapshot.get("scenarioHash") != scenario_hash(scenario):
        raise ValueError("snapshot does not belong to this unchanged scenario/due dates")
    now = _number(snapshot.get("currentTime"), "currentTime")
    if now > horizon(scenario):
        raise ValueError("currentTime exceeds the horizon")
    if not isinstance(snapshot.get("operations"), list) or not isinstance(snapshot.get("executedPrefix"), list) or not isinstance(snapshot.get("startCommitments"), list) or not isinstance(snapshot.get("events"), list):
        raise ValueError("snapshot requires operations, start commitments and an executed prefix")
    orders = {o["id"]: o for o in scenario["orders"]}
    expected = {(o["id"], u, s) for o in orders.values() for u in range(o["qty"]) for s in scenario["stages"]}
    actual, owners = {}, {}
    for operation in snapshot["operations"]:
        required = {"order", "unit", "stage", "state", "machine", "plannedPeriod", "plannedStart", "requiredHours", "processedHours", "remainingHours", "startedAt", "completedAt"}
        if not isinstance(operation, dict) or not required.issubset(operation):
            raise ValueError("incomplete operation state")
        if not isinstance(operation, dict) or not isinstance(operation.get("unit"), int) or isinstance(operation["unit"], bool):
            raise ValueError("operation unit must be an integer identity")
        if not isinstance(operation, dict) or key(operation) not in expected or key(operation) in actual:
            raise ValueError("snapshot contains missing/duplicate/unknown operation identities")
        actual[key(operation)] = operation
        if not isinstance(operation["state"], str) or operation["state"] not in {"unstarted", "wip", "completed"}:
            raise ValueError("unknown operation state")
        progress = _number(operation["processedHours"], "processedHours")
        if operation["machine"] is None:
            if operation["state"] != "unstarted" or progress or any(operation.get(field) is not None for field in
                 ("requiredHours", "remainingHours", "startedAt", "completedAt", "plannedStart", "plannedPeriod")):
                raise ValueError("an unassigned operation cannot have executed work")
            continue
        if not isinstance(operation["machine"], str):
            raise ValueError("invalid machine identity")
        rate = orders[operation["order"]]["hours"][operation["stage"]].get(operation["machine"])
        required_hours = _number(operation["requiredHours"], "requiredHours", positive=True)
        if rate is None or not math.isclose(required_hours, rate, abs_tol=EPS):
            raise ValueError("operation rate/machine is not eligible")
        remaining = _number(operation["remainingHours"], "remainingHours")
        _number(operation["plannedStart"], "plannedStart")
        if isinstance(operation["plannedPeriod"], bool) or not isinstance(operation["plannedPeriod"], int) or not 0 <= operation["plannedPeriod"] < len(scenario["periods"]):
            raise ValueError("plannedPeriod must belong to the horizon")
        if not math.isclose(progress + remaining, rate, abs_tol=EPS):
            raise ValueError("processing conservation failed")
        if operation["state"] == "completed":
            _number(operation["completedAt"], "completedAt")
            _number(operation["startedAt"], "startedAt")
            if remaining > EPS or operation["completedAt"] is None or operation["completedAt"] > now + EPS:
                raise ValueError("invalid completed operation")
        elif operation["state"] == "wip":
            _number(operation["startedAt"], "startedAt")
            if operation["startedAt"] is None or operation["startedAt"] > now + EPS or operation["completedAt"] is not None or remaining <= EPS:
                raise ValueError("invalid WIP operation")
            if operation["machine"] in owners:
                raise ValueError("two WIP operations cannot own the same machine")
            owners[operation["machine"]] = key(operation)
        elif progress or operation["startedAt"] is not None or operation["completedAt"] is not None:
            raise ValueError("unstarted operation has executed work")
    if set(actual) != expected:
        raise ValueError("snapshot must account for all demanded operations, including unassigned work")
    _validate_plan(scenario, {"assignment": [], "overtime": snapshot["overtime"]})
    events = []
    for event in snapshot["events"]:
        # Historical outages may precede currentTime, but never have invalid bounds.
        events.append(event_at({"currentTime": 0}, event, scenario))
    windows = calendars(scenario, snapshot["overtime"], events)
    commitments = {}
    for commitment in snapshot["startCommitments"]:
        identity = key(commitment)
        if identity not in actual or identity in commitments:
            raise ValueError("duplicate/unknown start commitment")
        operation = actual[identity]
        at = _number(commitment["time"], "commitment time")
        count = commitment["eventCount"]
        if isinstance(count, bool) or not isinstance(count, int) or not 0 <= count <= len(events):
            raise ValueError("invalid commitment event history")
        known = calendars(scenario, snapshot["overtime"], events[:count])
        if commitment["machine"] != operation["machine"] or operation["state"] == "unstarted" or at != operation["startedAt"] or at > now + EPS:
            raise ValueError("start commitment disagrees with the operation")
        if not any(w["start"] - EPS <= at < w["end"] - EPS for w in known[commitment["machine"]]):
            raise ValueError("operation committed outside its then-known calendar")
        commitments[identity] = commitment
    if set(commitments) != {identity for identity, operation in actual.items() if operation["state"] != "unstarted"}:
        raise ValueError("every started/completed operation needs its preserved commitment")
    processed, intervals, fab_finishes, operation_spans = {}, {}, {}, {}
    for operation in actual.values():
        if operation["stage"] == "fab" and operation["state"] == "completed":
            fab_finishes[(operation["order"], operation["unit"])] = operation["completedAt"]
    for segment in snapshot["executedPrefix"]:
        identity = key(segment)
        if not {"machine", "start", "end", "period", "overtimeHours"}.issubset(segment) or isinstance(segment["period"], bool) or not isinstance(segment["period"], int):
            raise ValueError("invalid executed segment shape/period")
        if identity not in actual or segment["machine"] != actual[identity]["machine"]:
            raise ValueError("prefix machine/identity disagrees with operation state")
        a, b = _number(segment["start"], "segment start"), _number(segment["end"], "segment end")
        if b <= a or b > now + EPS or not any(w["period"] == segment["period"] and a >= w["start"] - EPS and b <= w["end"] + EPS for w in windows[segment["machine"]]):
            raise ValueError("prefix is outside its legal calendar/cutoff")
        if segment["stage"] == "test" and a < fab_finishes.get(identity[:2], math.inf) - EPS:
            raise ValueError("test processing precedes completed fabrication")
        processed[identity] = processed.get(identity, 0) + b - a
        operation_spans.setdefault(identity, []).append((a, b))
        window = next(w for w in windows[segment["machine"]] if w["period"] == segment["period"] and a >= w["start"] - EPS and b <= w["end"] + EPS)
        if not math.isclose(_number(segment["overtimeHours"], "overtimeHours"), max(0, b - max(a, window["regularEnd"])), abs_tol=EPS):
            raise ValueError("prefix overtime disagrees with the authorized clock window")
        intervals.setdefault(segment["machine"], []).append((a, b))
    for identity, operation in actual.items():
        if not math.isclose(processed.get(identity, 0), operation["processedHours"], abs_tol=EPS):
            raise ValueError("prefix and processedHours disagree")
        spans = operation_spans.get(identity, [])
        if spans and min(a for a, _ in spans) < operation["startedAt"] - EPS:
            raise ValueError("startedAt disagrees with the executed prefix")
        if operation["state"] == "completed" and (not spans or abs(max(b for _, b in spans) - operation["completedAt"]) > EPS):
            raise ValueError("completedAt disagrees with the executed prefix")
    for spans in intervals.values():
        spans.sort()
        if any(a < previous_b - EPS for (_, previous_b), (a, _) in zip(spans, spans[1:])):
            raise ValueError("prefix machine intervals overlap")
    ownership = {}
    for operation in actual.values():
        if operation["state"] != "unstarted":
            stop = operation["completedAt"] if operation["state"] == "completed" else now
            if stop > operation["startedAt"]:
                ownership.setdefault(operation["machine"], []).append((operation["startedAt"], stop))
        if operation["stage"] == "test" and operation["state"] != "unstarted":
            predecessor = actual[(operation["order"], operation["unit"], "fab")]
            if predecessor["state"] != "completed" or operation["startedAt"] < predecessor["completedAt"] - EPS:
                raise ValueError("started testing requires completed fabrication")
    for intervals in ownership.values():
        intervals.sort()
        if any(a < previous_b - EPS for (_, previous_b), (a, _) in zip(intervals, intervals[1:])):
            raise ValueError("started operations retain exclusive machine ownership through pauses")
    return scenario


def resume(scenario, snapshot, events, *, placements=None, until=None):
    """Real SimPy continuation. placements=None retains the original FIFO routes.

    A provided placement schedule is checked independently before it is executed.
    State and history are copied, never reset or mutated in the caller.
    """
    scenario = validate_snapshot(scenario, snapshot)
    if not isinstance(events, list) or events[:len(snapshot["events"])] != snapshot["events"]:
        raise ValueError("existing calendar events are immutable")
    for event in events[len(snapshot["events"]):]:
        event_at(snapshot, event, scenario)
    state = deepcopy(snapshot)
    state["events"] = deepcopy(events)
    now = state["currentTime"]
    until = horizon(scenario) if until is None else _number(until, "until")
    if until < now or until > horizon(scenario):
        raise ValueError("resume cutoff must move forward within the horizon")
    windows = calendars(scenario, state["overtime"], events)
    operations = {key(o): o for o in state["operations"]}
    env = simpy.Environment(initial_time=now)
    machines = {m["id"]: simpy.Resource(env, capacity=1) for m in scenario["machines"]}
    finished = {identity: env.event() for identity in operations}
    for identity, operation in operations.items():
        if operation["state"] == "completed":
            finished[identity].succeed()
    active = {}
    planned = None if placements is None else {key(p): p for p in placements}
    if placements is not None:
        audit_placements(scenario, state, events, placements)
        for identity, operation in operations.items():
            if operation["state"] == "unstarted" and identity not in planned:
                for field in ("machine", "plannedPeriod", "plannedStart", "requiredHours", "remainingHours"):
                    operation[field] = None

    def execute(identity):
        operation = operations[identity]
        if operation["state"] == "completed":
            return
        if planned is not None:
            placement = planned.get(identity)
            if placement is None:
                return
            if operation["state"] == "unstarted":
                operation["machine"] = placement["machine"]
                operation["requiredHours"] = scenario_order[operation["order"]]["hours"][operation["stage"]][placement["machine"]]
                operation["remainingHours"] = operation["requiredHours"]
                operation["plannedPeriod"] = int(placement["holdStart"] / period_length(scenario))
                operation["plannedStart"] = placement["holdStart"]
            release = placement["holdStart"]
        else:
            if operation["machine"] is None:
                return
            release = now if operation["state"] == "wip" else max(now, operation["plannedStart"])
        if operation["stage"] == "test":
            yield finished[(identity[0], identity[1], "fab")]
        if env.now < release:
            yield env.timeout(release - env.now)
        with machines[operation["machine"]].request() as request:
            yield request
            if planned is not None:
                if abs(env.now - release) > EPS:
                    raise ValueError("placement violates resource/precedence timing")
                segments = placement["segments"]
            else:
                segments, _ = work(windows[operation["machine"]], env.now, operation["remainingHours"])
            for segment in segments:
                if env.now < segment["start"]:
                    yield env.timeout(segment["start"] - env.now)
                operation["state"] = "wip"
                if operation["startedAt"] is None:
                    operation["startedAt"] = env.now
                    state["startCommitments"].append({"order": identity[0], "unit": identity[1], "stage": identity[2],
                                                     "machine": operation["machine"], "time": env.now, "eventCount": len(events)})
                row = {"order": identity[0], "unit": identity[1], "stage": identity[2], "machine": operation["machine"], **segment}
                active[identity] = row
                yield env.timeout(segment["end"] - segment["start"])
                active.pop(identity)
                state["executedPrefix"].append(row)
                elapsed = segment["end"] - segment["start"]
                operation["processedHours"] += elapsed
                operation["remainingHours"] = max(0.0, operation["remainingHours"] - elapsed)
                if operation["remainingHours"] <= EPS:
                    operation["state"] = "completed"
                    operation["completedAt"] = env.now
                    finished[identity].succeed()
                    break

    scenario_order = {o["id"]: o for o in scenario["orders"]}
    for identity in sorted(operations, key=lambda i: (operations[i]["state"] != "wip", operations[i]["plannedPeriod"] or 0, i)):
        env.process(execute(identity))
    if until > now:
        env.run(until=until)
    # Process completions/start commitments exactly at the cutoff, not future work.
    while env.peek() <= until:
        env.step()
    for identity, row in active.items():
        elapsed = until - row["start"]
        if elapsed > EPS:
            partial = {**row, "end": until}
            window = next(w for w in windows[row["machine"]] if w["period"] == row["period"] and row["start"] >= w["start"] - EPS and row["end"] <= w["end"] + EPS)
            partial["overtimeHours"] = max(0.0, until - max(row["start"], window["regularEnd"]))
            state["executedPrefix"].append(partial)
            operations[identity]["processedHours"] += elapsed
            operations[identity]["remainingHours"] -= elapsed
    state["currentTime"] = until
    validate_snapshot(scenario, state)
    return state


def audit_placements(scenario, snapshot, events, placements):
    """Independent interval/rate/precedence/conservation checks, not solver rows."""
    operations = {key(o): o for o in snapshot["operations"]}
    orders = {o["id"]: o for o in scenario["orders"]}
    windows = calendars(scenario, snapshot["overtime"], events)
    chosen, machine_intervals = {}, {}
    for placement in placements:
        identity = key(placement)
        if identity not in operations or identity in chosen or operations[identity]["state"] == "completed":
            raise ValueError("cannot repeat completed/duplicate/unknown operations")
        operation = operations[identity]
        if placement["holdStart"] < snapshot["currentTime"] - EPS:
            raise ValueError("placement rewrites the executed past")
        if operation["state"] == "wip" and abs(placement["holdStart"] - snapshot["currentTime"]) > EPS:
            raise ValueError("WIP must retain ownership from the current time")
        rate = orders[identity[0]]["hours"][identity[2]].get(placement["machine"])
        if rate is None or (operation["state"] == "wip" and placement["machine"] != operation["machine"]):
            raise ValueError("WIP machine is frozen; future routes must be eligible")
        required = operation["remainingHours"] if operation["state"] == "wip" else rate
        expected, left = work(windows[placement["machine"]], placement["holdStart"], required)
        if placement["segments"] != expected or abs(placement["remainingHours"] - left) > EPS:
            raise ValueError("placement loses/invents processing or calendar time")
        if operation["state"] == "unstarted" and left > EPS:
            raise ValueError("unstarted operations must fit completely or stay unstarted")
        if operation["state"] == "unstarted" and (not expected or abs(expected[0]["start"] - placement["holdStart"]) > EPS):
            raise ValueError("unstarted work must start in a legal window")
        end = expected[-1]["end"] if left <= EPS and expected else math.inf
        chosen[identity] = end
        machine_intervals.setdefault(placement["machine"], []).append((placement["holdStart"], horizon(scenario) if left > EPS else end))
    for identity, operation in operations.items():
        if operation["state"] == "wip" and identity not in chosen:
            raise ValueError("a started operation cannot be dropped")
        if identity[2] == "test" and identity in chosen:
            predecessor = operations[(identity[0], identity[1], "fab")]
            ready = predecessor["completedAt"] if predecessor["state"] == "completed" else chosen.get((identity[0], identity[1], "fab"), math.inf)
            start = next(p["holdStart"] for p in placements if key(p) == identity)
            if start < ready - EPS:
                raise ValueError("test placement precedes completed fabrication")
    for intervals in machine_intervals.values():
        intervals.sort()
        if any(a < previous_b - EPS for (_, previous_b), (a, _) in zip(intervals, intervals[1:])):
            raise ValueError("placement machine ownership overlaps")
    return True


def metrics(scenario, snapshot):
    span = period_length(scenario)
    orders = {o["id"]: o for o in scenario["orders"]}
    finished = [o for o in snapshot["operations"] if o["stage"] == "test" and o["state"] == "completed"]
    on_time = sum(o["completedAt"] <= (orders[o["order"]]["due"] + 1) * span + EPS for o in finished)
    return {"onTimeUnits": on_time, "tardyUnits": len(finished) - on_time,
            "unfinishedUnits": sum(o["qty"] for o in orders.values()) - len(finished),
            "actualOvertimeHours": sum(s["overtimeHours"] for s in snapshot["executedPrefix"]),
            "reservedOvertimeHours": sum(r["hours"] for r in snapshot["overtime"])}
