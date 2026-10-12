"""Build/check an allowlisted recorded replay, never a hosted Python solver."""
from __future__ import annotations

import argparse
from copy import deepcopy
from datetime import datetime
import hashlib
import json
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from mpa.demo import ASSETS, INPUT, DemoSession, source_provenance
from mpa.scenario import load

FILES = {"index.html", "app.js", "style.css", "recording.json", "LICENSE"}


def canonical(value):
    # Unlike Python ==, JSON preserves bool-vs-number distinctions used by JS.
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def record(source=None):
    session = DemoSession(load(INPUT), source if source is not None else source_provenance())
    frames = []

    def capture(title, description):
        state, evidence = session.view(), session.export()
        state.pop("proposalId")
        state["mode"] = evidence["mode"] = "recorded"
        frames.append({"title": title, "description": description, "state": deepcopy(state), "evidence": deepcopy(evidence),
                       "evidenceJSON": json.dumps(evidence, ensure_ascii=False, indent=2, allow_nan=False) + "\n"})

    capture("1 · Execution frozen at hour 2", "A/0 is finished; A/1 has used 0.5 fabrication hours on M1, with 1 hour remaining.")
    session.compare({"revision": session.revision, "event": {"machine": "M1", "start": 2, "end": 6}})
    report = session.pending["report"]
    if (report["baseline"]["metrics"]["unfinishedUnits"] != 2 or not report["candidate"]
            or report["candidate"]["metrics"]["unfinishedUnits"] != 0):
        raise ValueError("the first recorded comparison no longer matches the four-unit example")
    capture("2 · Compare the M1 outage", "Real HiGHS and SimPy computations: original routes finish 2/4; the candidate finishes 4/4. Execution has not advanced.")
    session.advance({"revision": session.revision, "proposalId": session.pending["id"], "choice": "candidate", "confirmed": True, "until": 3.5})
    capture("3 · Confirm, then advance to hour 3.5", "New processing becomes immutable history. The original M1 work remains committed during the outage.")
    session.compare({"revision": session.revision, "event": {"machine": "M2", "start": 3.5, "end": 5.5}})
    capture("4 · A second event, from the new snapshot", "M2 now stops until hour 5.5. Started work stays on its machine; the second comparison cannot replay completed stages.")
    session.advance({"revision": session.revision, "proposalId": session.pending["id"], "choice": "candidate", "confirmed": True, "until": 8})
    if session.view()["metrics"]["onTimeUnits"] != 4:
        raise ValueError("the second recorded event no longer completes the four-unit example")
    capture("5 · Confirmed execution at hour 8", "All four units are complete. Both events, exact processing segments and decision hashes are retained in the downloadable evidence.")
    return {"schemaVersion": 1, "mode": "recorded", "computedBy": "mpa.demo.DemoSession / HiGHS / SimPy", "frames": frames}


def safe_output(path):
    path = path.resolve()
    protected = [ROOT / "results", ROOT / "scenarios", ROOT / "data", ROOT / "mpa", ROOT / "tests", ROOT / ".github"]
    if path == ROOT or any(path == p or p in path.parents or path in p.parents for p in protected):
        raise ValueError("use a separate build directory; original inputs/results/source are protected")
    if path.exists():
        existing = {p.relative_to(path).as_posix() for p in path.rglob("*") if p.is_file()}
        if existing - FILES - {"manifest.json"} or any(p.is_symlink() for p in path.rglob("*")):
            raise ValueError("output contains files outside this replay's allowlist")
    return path


def require_clean():
    if subprocess.run(["git", "status", "--porcelain"], cwd=ROOT, capture_output=True, text=True, check=True).stdout.strip():
        raise ValueError("build requires a clean source checkout")


def build(output):
    output = safe_output(output)
    output.mkdir(parents=True, exist_ok=True)
    html = (ASSETS / "index.html").read_text(encoding="utf-8").replace("__MODE__", "recorded").replace("__BOOTSTRAP__", "")
    (output / "index.html").write_text(html, encoding="utf-8", newline="\n")
    for name in ["app.js", "style.css"]:
        (output / name).write_text((ASSETS / name).read_text(encoding="utf-8"), encoding="utf-8", newline="\n")
    (output / "LICENSE").write_text((ROOT / "LICENSE").read_text(encoding="utf-8"), encoding="utf-8", newline="\n")
    recording = record()
    (output / "recording.json").write_text(json.dumps(recording, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8", newline="\n")
    manifest = {"schemaVersion": 1, "mode": "recorded", "source": recording["frames"][0]["state"]["provenance"],
                "files": {name: {"bytes": (output / name).stat().st_size, "sha256": hashlib.sha256((output / name).read_bytes()).hexdigest()} for name in sorted(FILES)}}
    (output / "manifest.json").write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8", newline="\n")
    return check(output)


def check(output):
    output = safe_output(output)
    if {p.relative_to(output).as_posix() for p in output.rglob("*") if p.is_file()} != FILES | {"manifest.json"}:
        raise ValueError("replay files do not match the exact allowlist")
    manifest = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
    if set(manifest["files"]) != FILES:
        raise ValueError("manifest does not match the allowlist")
    for name, row in manifest["files"].items():
        raw = (output / name).read_bytes()
        if row != {"bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}:
            raise ValueError("replay artifact checksum mismatch")
    current = source_provenance()
    # Generation time is metadata, not a new computation. All other provenance
    # fields (including dependencies and clean state) must match this checkout.
    observed_source = dict(manifest["source"])
    timestamp = observed_source.pop("generated_at")
    expected_source = dict(current); expected_source.pop("generated_at")
    if canonical(observed_source) != canonical(expected_source):
        raise ValueError("replay source fingerprint is stale")
    if datetime.fromisoformat(timestamp).tzinfo is None:
        raise ValueError("recorded generation time requires a timezone")
    recording = json.loads((output / "recording.json").read_text(encoding="utf-8"))
    if recording["mode"] != "recorded" or len(recording["frames"]) != 5:
        raise ValueError("recorded walkthrough is incomplete")
    html = (output / "index.html").read_text(encoding="utf-8")
    if 'data-mode="recorded"' not in html or "__BOOTSTRAP__" in html:
        raise ValueError("artifact must use the recorded-only interface")
    expected_html = (ASSETS / "index.html").read_text(encoding="utf-8").replace("__MODE__", "recorded").replace("__BOOTSTRAP__", "")
    expected_assets = {"index.html": expected_html, "LICENSE": (ROOT / "LICENSE").read_text(encoding="utf-8"),
                       **{name: (ASSETS / name).read_text(encoding="utf-8") for name in ("app.js", "style.css")}}
    for name, expected in expected_assets.items():
        if (output / name).read_bytes() != expected.encode("utf-8"):
            raise ValueError("replay asset differs from current source: " + name)
    # A rehashed forged metric/trace is still a forgery. Recompute using the real
    # current engine/input, reusing only the already checked provenance timestamp.
    if canonical(recording) != canonical(record(source=manifest["source"])):
        raise ValueError("recording differs from fresh current-source computation")
    return {"files": len(FILES) + 1, "bytes": sum(p.stat().st_size for p in output.iterdir()), "frames": 5, "sourceCommit": current["sourceCommit"]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "build" / "pages")
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--require-clean", action="store_true")
    args = parser.parse_args()
    if args.require_clean:
        require_clean()
    print(json.dumps(check(args.output) if args.check else build(args.output)))


if __name__ == "__main__":
    main()
