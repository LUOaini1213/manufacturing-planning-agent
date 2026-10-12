"""Local synthetic rolling-planning workbench. No model, uploads or live dispatch.

python -m mpa.demo --port 8840
"""
from __future__ import annotations

import argparse
from copy import deepcopy
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import math
from pathlib import Path
import secrets
import subprocess
import sys
import threading
import time
from urllib.parse import urlsplit

from mpa.reporting import provenance
from mpa.rolling import advance_confirmed, gantt, propose, snapshot_hash
from mpa.rolling_state import horizon, initial_snapshot, metrics
from mpa.scenario import load

ROOT = Path(__file__).resolve().parents[1]
ASSETS = Path(__file__).with_name("demo_assets")
INPUT = ROOT / "scenarios" / "rolling_outage.json"
MAX_BODY = 16_384
SESSION_TTL = 30 * 60
PROPOSAL_TTL = 5 * 60
MAX_SESSIONS = 24
MAX_EVENTS = 8


class DemoError(Exception):
    def __init__(self, status, code, message):
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


def exact_fields(body, fields):
    if not isinstance(body, dict) or set(body) != set(fields):
        raise DemoError(400, "invalid_request", "The request fields are not valid for this action.")


def number(value, label):
    try:
        valid = not isinstance(value, bool) and isinstance(value, (int, float)) and math.isfinite(value)
    except OverflowError:
        valid = False
    if not valid:
        raise DemoError(400, "invalid_time", f"{label} must be a finite number of hours.")
    return float(value)


def secure_equal(left, right):
    return isinstance(left, str) and isinstance(right, str) and left.isascii() and right.isascii() and secrets.compare_digest(left, right)


def source_provenance():
    """Hash both the engine and its presentation; disclose relative names only."""
    result = provenance([INPUT])
    extra = list(ASSETS.glob("*")) + [ROOT / "scripts" / "build_demo.py", ROOT / "requirements.txt"]
    for path in extra:
        if path.is_file():
            result["sha256"][path.relative_to(ROOT).as_posix()] = hashlib.sha256(path.read_text(encoding="utf-8").encode()).hexdigest()
    result["sourceHash"] = hashlib.sha256(json.dumps(result["sha256"], sort_keys=True).encode()).hexdigest()
    result["inputHash"] = result["sha256"][INPUT.relative_to(ROOT).as_posix()]
    try:
        result["sourceCommit"] = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True, check=True).stdout.strip()
        result["workingTreeClean"] = not subprocess.run(["git", "status", "--porcelain"], cwd=ROOT, capture_output=True, text=True, check=True).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        result["sourceCommit"], result["workingTreeClean"] = "unavailable", False
    return result


class BoundedSolver:
    """One fresh spawn at a time; native failure cannot take down the HTTP host."""
    def __init__(self, source_hash, *, timeout=25, command=None):
        self.source_hash, self.timeout = source_hash, timeout
        self.command = command or [sys.executable, "-m", "mpa.demo_worker"]
        self.lock = threading.Lock()
        self.last_pid = None
        self.unavailable = False
        self.quarantined = None

    def compute(self, scenario, snapshot, event, *, step):
        if not self.lock.acquire(blocking=False):
            raise DemoError(503, "solver_busy", "Another comparison is running. Wait briefly, then compare again.")
        process = None
        input_thread = None
        transfer_complete = False
        deadline = time.monotonic() + self.timeout  # includes spawn, transfer and exit
        try:
            if self.unavailable:
                raise DemoError(503, "solver_failed", "The solver worker could not stop. Restart this local workbench before comparing again.")
            process = subprocess.Popen(self.command, cwd=ROOT, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
            self.last_pid = process.pid
            raw = json.dumps({"scenario": scenario, "snapshot": snapshot, "event": event, "step": step}, allow_nan=False).encode()
            # Windows communicate(input=...) writes stdin synchronously before
            # applying its timeout. Supervise a writer so a child that never
            # reads its input cannot hold the HTTP request beyond the budget.
            input_stream = process.stdin
            process.stdin = None
            def feed():
                try:
                    input_stream.write(raw)
                except OSError:
                    pass
                finally:
                    try:
                        input_stream.close()
                    except OSError:
                        pass
            input_thread = threading.Thread(target=feed, daemon=True, name="planning-input")
            input_thread.start()
            output, _ = process.communicate(timeout=max(0, deadline - time.monotonic()))
            transfer_complete = True
            input_thread.join(max(0, deadline - time.monotonic()))
            if input_thread.is_alive():
                raise DemoError(504, "solver_timeout", "The comparison time limit was reached. Execution is unchanged.")
            if process.returncode != 0 or len(output) > 1_000_000:
                raise DemoError(503, "solver_failed", "The solver process could not complete. Execution is unchanged; compare again to retry.")
            try:
                payload = strict_json(output)
            except DemoError:
                raise DemoError(503, "solver_failed", "The solver response was incomplete. Execution is unchanged.") from None
            if not isinstance(payload, dict) or "report" not in payload:
                raise DemoError(503, "solver_failed", "The solver response was incomplete. Execution is unchanged.")
            if payload.get("sourceHash") != self.source_hash:
                raise DemoError(409, "source_changed", "The source changed after this workbench started. Restart it before comparing again.")
            return payload["report"]
        except subprocess.TimeoutExpired:
            raise DemoError(504, "solver_timeout", "The comparison time limit was reached. Execution is unchanged; compare again to retry.") from None
        except OSError:
            raise DemoError(503, "solver_failed", "The solver process could not complete. Execution is unchanged; compare again to retry.") from None
        finally:
            try:
                if process is not None:
                    cleanup_until = time.monotonic() + 2
                    if process.poll() is None:
                        try:
                            process.kill()
                        except OSError:
                            self.unavailable, self.quarantined = True, process
                    if not transfer_complete:
                        try:
                            # An exited worker can still have a descendant keeping
                            # stdout open. Only completed readers are safe to close.
                            process.communicate(timeout=max(0, cleanup_until - time.monotonic()))
                            transfer_complete = True
                        except (OSError, subprocess.TimeoutExpired, RuntimeError):
                            self.unavailable, self.quarantined = True, process
                    if input_thread and input_thread.is_alive():
                        input_thread.join(max(0, cleanup_until - time.monotonic()))
                        if input_thread.is_alive():
                            self.unavailable, self.quarantined = True, process
                    if not self.unavailable and transfer_complete:
                        # communicate completed; reader threads no longer hold pipe locks.
                        for stream in (process.stdin, process.stdout, process.stderr):
                            if stream is not None:
                                stream.close()
            finally:
                self.lock.release()


class DemoSession:
    """One page's state; locks and revisions cover solve + commit, not just writes."""
    def __init__(self, scenario, source, compute=None):
        self.scenario, self.source = deepcopy(scenario), deepcopy(source)
        self.compute = compute
        self.lock = threading.RLock()
        self.revision = 0
        self.snapshot = initial_snapshot(scenario, scenario["rolling"]["initialPlan"], scenario["rolling"]["currentTime"])
        self.pending = None
        self.comparison = None
        self.history = []
        self.notice = "Ready at hour 2. Introduce a machine outage to compare the remaining work."
        self.last_seen = time.monotonic()

    def fork(self):
        with self.lock:
            new = DemoSession(self.scenario, self.source, self.compute)
            for name in ("revision", "snapshot", "pending", "comparison", "history", "notice"):
                setattr(new, name, deepcopy(getattr(self, name)))
            if new.pending:
                new.pending["id"] = secrets.token_urlsafe(24)
            return new

    def expire_proposal(self):
        if self.pending and time.monotonic() - self.pending["created"] > PROPOSAL_TTL:
            self.pending = None
            self.comparison["phase"] = "expired"
            self.revision += 1
            self.notice = "The proposal expired. Compare the event again before confirming. Execution is unchanged."

    def check_revision(self, revision):
        self.expire_proposal()
        if isinstance(revision, bool) or not isinstance(revision, int) or revision != self.revision:
            raise DemoError(409, "stale_revision", "This view is out of date. Refresh the state before acting.")

    def compare(self, body):
        exact_fields(body, {"revision", "event"})
        self.check_revision(body["revision"])
        if len(self.snapshot["events"]) >= MAX_EVENTS:
            raise DemoError(409, "event_limit", "This small example allows eight events. Restart to begin another exercise.")
        exact_fields(body["event"], {"machine", "start", "end"})
        event = body["event"]
        if not isinstance(event["machine"], str) or event["machine"] not in {m["id"] for m in self.scenario["machines"]}:
            raise DemoError(400, "invalid_machine", "Choose a machine from this example.")
        start, end = number(event["start"], "Outage start"), number(event["end"], "Outage end")
        if not self.snapshot["currentTime"] <= start < end <= horizon(self.scenario):
            raise DemoError(400, "invalid_event", "The outage must be in the future and within the 10-hour example.")
        report = (self.compute or propose)(self.scenario, self.snapshot, {"machine": event["machine"], "start": start, "end": end}, step=0.5)
        self.pending = {"id": secrets.token_urlsafe(24), "created": time.monotonic(), "report": report}
        self.comparison = {"report": report, "phase": "pending", "choice": None, "advancedTo": None}
        self.revision += 1
        self.notice = "Comparison ready. Execution has not changed; choose and confirm a route to advance."
        return self.view()

    def advance(self, body):
        exact_fields(body, {"revision", "proposalId", "choice", "confirmed", "until"})
        self.check_revision(body["revision"])
        if not self.pending or not isinstance(body["proposalId"], str) or not secrets.compare_digest(body["proposalId"], self.pending["id"]):
            raise DemoError(409, "stale_proposal", "This proposal is no longer pending. Compare the event again.")
        if body["confirmed"] is not True:
            raise DemoError(400, "confirmation_required", "Explicit confirmation is required to advance the simulation.")
        if body["choice"] not in ("candidate", "original"):
            raise DemoError(400, "invalid_choice", "Choose original routes or the recommended candidate.")
        report = self.pending["report"]
        if body["choice"] == "candidate" and report["recommendation"] != "candidate":
            raise DemoError(409, "candidate_unavailable", "Keep original routes: there is no recommended candidate to adopt.")
        until = number(body["until"], "Advance time")
        if not self.snapshot["currentTime"] < until <= horizon(self.scenario):
            raise DemoError(400, "invalid_time", "Advance beyond the current time, within the 10-hour example.")
        next_state = advance_confirmed(self.scenario, self.snapshot, report, until, choice=body["choice"], confirmed=True)
        self.history.append({"event": report["events"][-1], "choice": body["choice"], "from": self.snapshot["currentTime"],
                             "until": until, "beforeHash": snapshot_hash(self.snapshot), "afterHash": snapshot_hash(next_state),
                             "report": deepcopy(report)})
        self.snapshot = next_state
        self.pending = None  # one adoption only, even when the client loses the response
        self.comparison = {"report": report, "phase": "confirmed", "choice": body["choice"], "advancedTo": until}
        self.revision += 1
        self.notice = f"Confirmed {body['choice']} routes. SimPy advanced to hour {until:g}; the executed prefix is now frozen."
        return self.view()

    def reset(self, body):
        exact_fields(body, {"revision"})
        self.check_revision(body["revision"])
        self.snapshot = initial_snapshot(self.scenario, self.scenario["rolling"]["initialPlan"], self.scenario["rolling"]["currentTime"])
        self.pending, self.comparison, self.history = None, None, []
        self.revision += 1
        self.notice = "Example restarted at hour 2. Previous proposals can no longer be confirmed."
        return self.view()

    def view(self):
        self.expire_proposal()
        comparison = None
        if self.comparison:
            comparison = deepcopy(self.comparison)
            comparison["svg"] = gantt(self.scenario, comparison["report"])
            if comparison["phase"] != "pending":
                comparison["svg"] = comparison["svg"].replace("HiGHS future proposal (not yet confirmed)", "Recorded HiGHS comparison")
        return {"schemaVersion": 1, "mode": "local", "revision": self.revision, "snapshot": deepcopy(self.snapshot),
                "snapshotHash": snapshot_hash(self.snapshot), "metrics": metrics(self.scenario, self.snapshot),
                "horizon": horizon(self.scenario), "machines": [m["id"] for m in self.scenario["machines"]],
                "defaultEvent": deepcopy(self.scenario["rolling"]["event"]), "comparison": comparison,
                "proposalId": self.pending["id"] if self.pending else None,
                "history": [{k: deepcopy(v) for k, v in row.items() if k != "report"} for row in self.history],
                "notice": self.notice, "provenance": deepcopy(self.source)}

    def export(self):
        """Exact evidence, deliberately without transport credentials or private paths."""
        result = self.view()
        result.pop("proposalId")
        result["scenario"] = deepcopy(self.scenario)
        result["history"] = deepcopy(self.history)
        result["model_calls"] = 0
        result["live_dispatch"] = False
        return result


class SessionStore:
    def __init__(self, scenario, source, compute=None):
        self.scenario, self.source, self.compute = scenario, source, compute
        self.lock = threading.RLock()
        self.sessions = {}

    def open(self, body):
        exact_fields(body, {"resumeId", "resumeToken"})
        if any(value is not None and (not isinstance(value, str) or len(value) > 100) for value in body.values()):
            raise DemoError(400, "invalid_resume", "The previous local exercise could not be identified.")
        with self.lock:
            now = time.monotonic()
            self.sessions = {sid: row for sid, row in self.sessions.items() if now - row["session"].last_seen <= SESSION_TTL}
            previous = self.sessions.get(body["resumeId"])
        # Never hold the registry lock while waiting for another page's action.
        if previous and secure_equal(previous["token"], body["resumeToken"]):
            if not previous["session"].lock.acquire(blocking=False):
                raise DemoError(409, "action_running", "An action is still running in the previous exercise. Reload shortly to restore it.")
            try:
                session = previous["session"].fork()
            finally:
                previous["session"].lock.release()
        else:
            session = DemoSession(self.scenario, self.source, self.compute)
            if body["resumeId"]:
                session.notice = "The previous local exercise is unavailable. A new example is ready at hour 2."
        with self.lock:
            # Each page load forks the state, so duplicate-tab sessionStorage cannot share mutations.
            if len(self.sessions) >= MAX_SESSIONS:
                oldest = min(self.sessions, key=lambda sid: self.sessions[sid]["session"].last_seen)
                del self.sessions[oldest]
            sid, token = secrets.token_urlsafe(24), secrets.token_urlsafe(32)
            self.sessions[sid] = {"session": session, "token": token}
            with session.lock:
                return {**session.view(), "sessionId": sid, "token": token}

    def get(self, sid, token):
        with self.lock:
            row = self.sessions.get(sid)
            if not row or time.monotonic() - row["session"].last_seen > SESSION_TTL:
                raise DemoError(410, "session_expired", "This local exercise expired. Reload to start or restore an available exercise.")
            if not secure_equal(row["token"], token):
                raise DemoError(403, "access_denied", "The local request could not be authorized. Reload the page.")
            row["session"].last_seen = time.monotonic()
            return row["session"]


def strict_json(raw):
    def pairs(items):
        result = {}
        for k, v in items:
            if k in result:
                raise ValueError("duplicate field")
            result[k] = v
        return result
    def nonfinite(_):
        raise ValueError("nonfinite JSON")
    try:
        return json.loads(raw.decode("utf-8"), object_pairs_hook=pairs, parse_constant=nonfinite)
    except (ValueError, UnicodeError):
        raise DemoError(400, "invalid_json", "Send a valid JSON object with finite numbers and unique fields.") from None


def create_server(port=8840):
    scenario, source = load(INPUT), source_provenance()
    solver = BoundedSolver(source["sourceHash"])
    store, bootstrap = SessionStore(scenario, source, solver.compute), secrets.token_urlsafe(32)
    assets = {"/": (ASSETS / "index.html").read_bytes(), "/app.js": (ASSETS / "app.js").read_bytes(),
              "/style.css": (ASSETS / "style.css").read_bytes()}
    assets["/"] = assets["/"].replace(b"__MODE__", b"local").replace(b"__BOOTSTRAP__", bootstrap.encode())

    class Handler(BaseHTTPRequestHandler):
        server_version = "PlanningDemo"

        def log_message(self, *_):
            pass

        def reply(self, status, payload, content_type="application/json", attachment=None):
            raw = json.dumps(payload, allow_nan=False).encode() if content_type == "application/json" else payload
            self.send_response(status)
            self.send_header("Content-Type", content_type + "; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; object-src 'none'; base-uri 'none'")
            if attachment:
                self.send_header("Content-Disposition", f'attachment; filename="{attachment}"')
            self.end_headers()
            self.wfile.write(raw)

        def guards(self, mutation=False):
            hosts = self.headers.get_all("Host", [])
            allowed = {f"127.0.0.1:{self.server.server_port}", f"localhost:{self.server.server_port}"}
            if len(hosts) != 1 or hosts[0] not in allowed:
                raise DemoError(403, "invalid_host", "Use the local workbench address.")
            origins = self.headers.get_all("Origin", [])
            if len(origins) > 1:
                raise DemoError(403, "invalid_origin", "Use the workbench in its local browser tab.")
            origin = origins[0] if origins else None
            if (origin is not None and origin != f"http://{hosts[0]}") or (mutation and origin is None):
                raise DemoError(403, "invalid_origin", "Use the workbench in its local browser tab.")
            if self.headers.get("Sec-Fetch-Site") not in (None, "same-origin", "none"):
                raise DemoError(403, "invalid_origin", "Use the workbench in its local browser tab.")

        def do_GET(self):
            try:
                self.guards()
                path = urlsplit(self.path)
                if path.query or path.fragment:
                    raise DemoError(404, "not_found", "This page is not part of the workbench.")
                if path.path in assets:
                    kind = "text/html" if path.path == "/" else "text/javascript" if path.path.endswith(".js") else "text/css"
                    self.reply(200, assets[path.path], kind)
                elif path.path in ("/api/state", "/api/export"):
                    session = store.get(self.headers.get("X-Demo-Session"), self.headers.get("X-Demo-Token"))
                    if not session.lock.acquire(blocking=False):
                        raise DemoError(409, "action_running", "An action is running in this exercise. Refresh state shortly.")
                    try:
                        self.reply(200, session.export() if path.path == "/api/export" else session.view(), attachment="planning-evidence.json" if path.path == "/api/export" else None)
                    finally:
                        session.lock.release()
                else:
                    raise DemoError(404, "not_found", "This page is not part of the workbench.")
            except DemoError as error:
                self.reply(error.status, {"error": {"code": error.code, "message": error.message}})

        def do_POST(self):
            self.connection.settimeout(5)
            try:
                self.guards(mutation=True)
                if self.headers.get("Transfer-Encoding") or len(self.headers.get_all("Content-Length", [])) != 1:
                    raise DemoError(400, "invalid_body", "A bounded JSON body is required.")
                try:
                    size = int(self.headers["Content-Length"])
                except ValueError:
                    raise DemoError(400, "invalid_body", "A bounded JSON body is required.") from None
                if not 0 < size <= MAX_BODY:
                    raise DemoError(413, "body_too_large", "The request exceeds this small example's limit.")
                if self.headers.get("Content-Type", "").split(";")[0].strip() != "application/json":
                    raise DemoError(415, "invalid_body", "Send a JSON request.")
                raw = self.rfile.read(size)
                if len(raw) != size:
                    raise DemoError(400, "invalid_body", "The JSON request was incomplete.")
                body = strict_json(raw)
                if self.path == "/api/session":
                    if not secure_equal(self.headers.get("X-Demo-Token"), bootstrap):
                        raise DemoError(403, "access_denied", "Reload the local workbench page.")
                    self.reply(200, store.open(body))
                    return
                action = {"/api/propose": "compare", "/api/advance": "advance", "/api/reset": "reset"}.get(self.path)
                if not action:
                    raise DemoError(404, "not_found", "This action is not part of the workbench.")
                session = store.get(self.headers.get("X-Demo-Session"), self.headers.get("X-Demo-Token"))
                if not session.lock.acquire(blocking=False):
                    raise DemoError(409, "action_running", "An action is running in this exercise. Refresh state shortly.")
                try:
                    self.reply(200, getattr(session, action)(body))
                finally:
                    session.lock.release()
            except DemoError as error:
                self.reply(error.status, {"error": {"code": error.code, "message": error.message}})
            except ValueError:
                self.reply(400, {"error": {"code": "invalid_event", "message": "This event could not be audited. Execution is unchanged; check the event times."}})
            except (OSError, TimeoutError):
                self.close_connection = True
            except Exception:
                self.reply(500, {"error": {"code": "solver_error", "message": "The comparison could not be completed. Execution is unchanged; refresh and try again."}})

    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    server.daemon_threads = True
    server.demo_store = store
    server.demo_solver = solver
    return server


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8840)
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("port must be between 1 and 65535")
    server = create_server(args.port)
    print(f"Synthetic planning workbench: http://127.0.0.1:{args.port} (no API key; Ctrl+C to stop)", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
