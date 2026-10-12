"""Real loopback HTTP + HiGHS/SimPy, isolated in-memory exercises only."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import http.client
import json
import subprocess
import sys
import re
import socket
import threading
import time

import pytest

from mpa import demo


@pytest.fixture(scope="module")
def server():
    instance = demo.create_server(0)
    thread = threading.Thread(target=lambda: instance.serve_forever(poll_interval=0.05), daemon=True)
    thread.start()
    yield instance
    instance.shutdown()
    instance.server_close()
    thread.join(timeout=3)
    assert not thread.is_alive()


def request(server, path, body=None, *, session=None, headers=None, raw=None):
    port = server.server_port
    base = {"Origin": f"http://127.0.0.1:{port}"}
    if session:
        base.update({"X-Demo-Session": session["sessionId"], "X-Demo-Token": session["token"]})
    if body is not None or raw is not None:
        base["Content-Type"] = "application/json"
    base.update(headers or {})
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=35)
    try:
        connection.request("GET" if body is None and raw is None else "POST", path,
                           body=raw if raw is not None else json.dumps(body) if body is not None else None,
                           headers=base)
        result = connection.getresponse()
        data = result.read()
        return result.status, json.loads(data) if "application/json" in result.getheader("Content-Type") else data.decode(), dict(result.getheaders())
    finally:
        connection.close()


def open_session(server, previous=None):
    status, page, headers = request(server, "/")
    assert status == 200 and "frame-ancestors 'none'" in headers["Content-Security-Policy"]
    bootstrap = re.search(r'name="demo-bootstrap" content="([^"]+)"', page)[1]
    status, state, _ = request(server, "/api/session", {"resumeId": previous["sessionId"] if previous else None,
                           "resumeToken": previous["token"] if previous else None}, headers={"X-Demo-Token": bootstrap})
    assert status == 200
    return state


def compare(server, state, event=None):
    status, result, _ = request(server, "/api/propose", {"revision": state["revision"], "event": event or {"machine": "M1", "start": 2, "end": 6}}, session=state)
    assert status == 200, result
    return {**result, "sessionId": state["sessionId"], "token": state["token"]}


def confirmation(state, until=3.5, choice="candidate"):
    return {"revision": state["revision"], "proposalId": state["proposalId"], "choice": choice, "confirmed": True, "until": until}


def test_http_two_events_real_execution_and_exact_export(server):
    original = open_session(server)
    frozen = deepcopy(original["snapshot"])
    state = compare(server, original)
    report = state["comparison"]["report"]
    assert state["snapshot"] == frozen  # actual HTTP comparison cannot advance execution
    assert report["baseline"]["metrics"]["unfinishedUnits"] == 2
    assert report["candidate"]["metrics"]["onTimeUnits"] == 4
    assert report["candidate"]["metrics"]["unfinishedUnits"] == 0
    assert report["model_calls"] == 0 and report["needs_human_confirm"] is True
    denied = confirmation(state); denied["confirmed"] = False
    assert request(server, "/api/advance", denied, session=state)[0] == 400
    status, actual, _ = request(server, "/api/advance", confirmation(state), session=state)
    assert status == 200
    assert actual["snapshot"]["currentTime"] == 3.5
    assert actual["snapshot"]["executedPrefix"][:len(frozen["executedPrefix"])] == frozen["executedPrefix"]
    assert request(server, "/api/advance", confirmation(state), session=state)[0] == 409
    state = {**actual, "sessionId": original["sessionId"], "token": original["token"]}
    state = compare(server, state, {"machine": "M2", "start": 3.5, "end": 5.5})
    assert len(state["comparison"]["report"]["events"]) == 2
    status, final, _ = request(server, "/api/advance", confirmation(state, until=8), session=state)
    assert status == 200 and final["metrics"]["onTimeUnits"] == 4
    assert final["metrics"]["unfinishedUnits"] == 0
    assert len(final["snapshot"]["operations"]) == 8 and len(final["history"]) == 2
    a0 = next(o for o in final["snapshot"]["operations"] if (o["order"], o["unit"], o["stage"]) == ("A", 0, "test"))
    assert a0["completedAt"] == 2
    a1 = next(o for o in final["snapshot"]["operations"] if (o["order"], o["unit"], o["stage"]) == ("A", 1, "fab"))
    assert a1["machine"] == "M1" and a1["processedHours"] == 1.5
    status, evidence, headers = request(server, "/api/export", session=state)
    assert status == 200 and "attachment" in headers["Content-Disposition"]
    assert evidence["snapshot"] == final["snapshot"] and len(evidence["history"]) == 2
    assert evidence["scenario"]["orders"][0]["due"] == 0 and evidence["snapshot"]["overtime"] == []
    raw = json.dumps(evidence)
    assert not any(secret in raw for secret in [state["token"], state["sessionId"], state["proposalId"]])
    assert all(not name.startswith(("/", "C:", "E:")) for name in evidence["provenance"]["sha256"])
    assert "mpa/demo_assets/app.js" in evidence["provenance"]["sha256"]
    assert "mpa/demo.py" in evidence["provenance"]["sha256"]


def test_refresh_and_duplicate_tabs_fork_without_sharing_decisions(server):
    original = compare(server, open_session(server))
    one, two = open_session(server, original), open_session(server, original)
    assert one["snapshot"] == original["snapshot"] == two["snapshot"]
    assert len({original["sessionId"], one["sessionId"], two["sessionId"]}) == 3
    assert len({original["proposalId"], one["proposalId"], two["proposalId"]}) == 3
    assert request(server, "/api/advance", confirmation(original), session=one)[0] == 409
    assert request(server, "/api/advance", confirmation(one), session=one)[0] == 200
    status, still_pending, _ = request(server, "/api/state", session=two)
    assert status == 200 and still_pending["snapshot"]["currentTime"] == 2
    assert still_pending["comparison"]["phase"] == "pending"


def test_racing_admissions_accept_one_revision_and_not_two_events(server):
    state = open_session(server)
    body = {"revision": state["revision"], "event": {"machine": "M1", "start": 2, "end": 6}}
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: request(server, "/api/propose", body, session=state), range(2)))
    assert sorted(result[0] for result in results) == [200, 409]
    actual = request(server, "/api/state", session=state)[1]
    assert actual["revision"] == 1 and len(actual["comparison"]["report"]["events"]) == 1
    assert actual["snapshot"] == state["snapshot"]


@pytest.mark.parametrize("event", [
    {"machine": "M1", "start": False, "end": 6}, {"machine": "M1", "start": 1, "end": 6},
    {"machine": "M1", "start": 2, "end": 11}, {"machine": "other", "start": 2, "end": 6},
])
def test_invalid_event_is_not_committed(server, event):
    state = open_session(server)
    assert request(server, "/api/propose", {"revision": 0, "event": event}, session=state)[0] == 400
    actual = request(server, "/api/state", session=state)[1]
    assert actual["revision"] == 0 and actual["snapshot"] == state["snapshot"] and actual["comparison"] is None


def test_http_host_origin_token_body_and_no_paths(server):
    state = open_session(server)
    body = {"revision": 0, "event": {"machine": "M1", "start": 2, "end": 6}}
    for headers in [{"Host": "evil.example"}, {"Origin": "https://evil.example"}, {"X-Demo-Token": "wrong"}, {"Sec-Fetch-Site": "cross-site"}]:
        assert request(server, "/api/propose", body, session=state, headers=headers)[0] == 403
    for raw in [b'{"revision":NaN,"event":{}}', b'{"revision":Infinity,"event":{}}', b'{"revision":0,"revision":1,"event":{}}']:
        assert request(server, "/api/propose", session=state, raw=raw)[0] == 400
    # The size guard rejects declared length before reading a body. Avoid racing
    # a large client write against that deliberate early connection close.
    oversized = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)
    try:
        oversized.putrequest("POST", "/api/propose")
        for key, value in {"Origin": f"http://127.0.0.1:{server.server_port}",
                           "Content-Type": "application/json", "Content-Length": str(demo.MAX_BODY + 1),
                           "X-Demo-Session": state["sessionId"], "X-Demo-Token": state["token"]}.items():
            oversized.putheader(key, value)
        oversized.endheaders()
        oversized.sock.shutdown(socket.SHUT_WR)
        result = oversized.getresponse()
        assert result.status == 413
        result.read()
    finally:
        oversized.close()
    assert request(server, "/api/propose", {**body, "path": "C:/private"}, session=state)[0] == 400
    assert request(server, "/../../README.md")[0] == 404
    assert request(server, "/api/state?path=README.md", session=state)[0] == 404
    assert request(server, "/api/state", session=state)[1]["revision"] == 0
    connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)
    connection.request("POST", "/api/propose", json.dumps(body), headers={"Content-Type": "application/json", "X-Demo-Session": state["sessionId"], "X-Demo-Token": state["token"]})
    result = connection.getresponse()
    assert result.status == 403  # a valid token alone cannot replace same-origin admission
    result.read(); connection.close()


@pytest.mark.parametrize("worse", [False, True])
def test_no_or_worse_candidate_cannot_be_confirmed_but_original_can(server, monkeypatch, worse):
    real = server.demo_store.compute
    def controlled(*args, **kwargs):
        report = real(*args, **kwargs)
        report["recommendation"] = "keep_original"
        report["reason"] = "controlled conservative-grid regression" if worse else "controlled solver limit"
        if not worse:
            report["solver"] = {"ok": False, "status": "time_limit"}; report["candidate"] = None
        return report
    monkeypatch.setattr(server.demo_store, "compute", controlled)
    state = compare(server, open_session(server))
    assert state["comparison"]["report"]["recommendation"] == "keep_original"
    assert request(server, "/api/advance", confirmation(state), session=state)[0] == 409
    assert request(server, "/api/state", session=state)[1]["snapshot"] == state["snapshot"]
    status, actual, _ = request(server, "/api/advance", confirmation(state, choice="original"), session=state)
    assert status == 200 and actual["snapshot"]["currentTime"] == 3.5


def test_expired_proposal_reset_and_session_memory_bounds(server):
    state = compare(server, open_session(server))
    server.demo_store.sessions[state["sessionId"]]["session"].pending["created"] -= demo.PROPOSAL_TTL + 1
    assert request(server, "/api/advance", confirmation(state), session=state)[0] == 409
    actual = request(server, "/api/state", session=state)[1]
    assert actual["comparison"]["phase"] == "expired" and actual["snapshot"] == state["snapshot"]
    reset = request(server, "/api/reset", {"revision": actual["revision"]}, session=state)[1]
    assert reset["revision"] > state["revision"] and reset["comparison"] is None
    assert request(server, "/api/advance", confirmation(state), session=state)[0] == 409
    for _ in range(demo.MAX_SESSIONS + 1):
        open_session(server)
    assert len(server.demo_store.sessions) == demo.MAX_SESSIONS


def test_unknown_solver_error_does_not_leak_or_advance(server, monkeypatch):
    def broken(*_, **__):
        raise RuntimeError("C:/private-secret/data")
    monkeypatch.setattr(server.demo_store, "compute", broken)
    state = open_session(server)
    status, data, _ = request(server, "/api/propose", {"revision": 0, "event": {"machine": "M1", "start": 2, "end": 6}}, session=state)
    assert status == 500 and "private-secret" not in json.dumps(data)
    assert request(server, "/api/state", session=state)[1]["snapshot"] == state["snapshot"]


@pytest.mark.parametrize("code,timeout,expected", [("import os; os._exit(71)", 10, 503), ("import time; time.sleep(10)", 0.1, 504)])
def test_real_spawn_failure_or_budget_preserves_host_state_and_reaps_child(server, monkeypatch, code, timeout, expected):
    launched = []
    original = demo.subprocess.Popen
    def captured(*args, **kwargs):
        child = original(*args, **kwargs); launched.append(child); return child
    monkeypatch.setattr(demo.subprocess, "Popen", captured)
    solver = demo.BoundedSolver(server.demo_solver.source_hash, command=[sys.executable, "-c", code], timeout=timeout)
    monkeypatch.setattr(server.demo_store, "compute", solver.compute)
    state = open_session(server)
    start = time.monotonic()
    status, data, _ = request(server, "/api/propose", {"revision": 0, "event": {"machine": "M1", "start": 2, "end": 6}}, session=state)
    assert status == expected and data["error"]["code"] in {"solver_failed", "solver_timeout"}
    assert time.monotonic() - start < timeout + 4
    assert solver.last_pid is not None and launched[0].poll() is not None
    assert not any(t.name == "planning-input" and t.is_alive() for t in threading.enumerate())
    actual = request(server, "/api/state", session=state)[1]
    assert actual["snapshot"] == state["snapshot"] and actual["comparison"] is None and actual["revision"] == 0


def test_exited_worker_with_inherited_stdout_is_bounded_and_quarantined(server, monkeypatch, tmp_path):
    # Real descendant retains stdout after the worker exits; Windows readers
    # must not be closed under their held lock when bounded drain expires.
    marker = tmp_path / "descendant-started.txt"
    code = ("import subprocess,sys; from pathlib import Path; "
            "p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(6)'],stdin=subprocess.DEVNULL,stdout=sys.stdout,stderr=sys.stderr); "
            "Path(sys.argv[1]).write_text(str(p.pid),encoding='utf-8')")
    solver = demo.BoundedSolver(server.demo_solver.source_hash, command=[sys.executable, "-c", code, str(marker)], timeout=1)
    monkeypatch.setattr(server.demo_store, "compute", solver.compute)
    state = open_session(server)
    started = time.monotonic()
    body = {"revision": 0, "event": {"machine": "M1", "start": 2, "end": 6}}
    try:
        assert request(server, "/api/propose", body, session=state)[0] == 504
        assert time.monotonic() - started < 4
        assert int(marker.read_text(encoding="utf-8")) > 0  # reached the inherited-pipe condition
        assert solver.unavailable and solver.quarantined.poll() is not None
        assert request(server, "/api/propose", body, session=state)[0] == 503
        assert request(server, "/api/state", session=state)[1]["snapshot"] == state["snapshot"]
    finally:
        if solver.quarantined:
            solver.quarantined.communicate(timeout=8)  # synthetic descendant expires on its own
            for stream in (solver.quarantined.stdout, solver.quarantined.stderr):
                if stream:
                    stream.close()
