"""Publication contains only a real synthetic recording and its checked assets."""
import hashlib
import json
from pathlib import Path

import pytest

from scripts.build_demo import ROOT, build, check


def test_recording_uses_actual_two_events_and_keeps_original_artifacts(tmp_path):
    originals = {p: p.read_bytes() for folder in ["scenarios", "results"] for p in (ROOT / folder).rglob("*") if p.is_file()}
    result = build(tmp_path / "replay")
    assert result["files"] == 6 and result["frames"] == 5
    assert check(tmp_path / "replay") == result
    recorded = json.loads((tmp_path / "replay" / "recording.json").read_text(encoding="utf-8"))
    assert recorded["mode"] == "recorded"
    assert [f["state"]["snapshot"]["currentTime"] for f in recorded["frames"]] == [2, 2, 3.5, 3.5, 8]
    first = recorded["frames"][1]["evidence"]["comparison"]["report"]
    assert first["baseline"]["metrics"]["unfinishedUnits"] == 2 and first["candidate"]["metrics"]["unfinishedUnits"] == 0
    last = recorded["frames"][-1]["evidence"]
    assert last["metrics"]["onTimeUnits"] == 4 and len(last["history"]) == 2
    assert recorded["frames"][0]["state"]["snapshot"]["executedPrefix"] == last["snapshot"]["executedPrefix"][:3]
    assert not any(word in json.dumps(recorded) for word in ['"token"', '"sessionId"', '"proposalId"', '"resumeToken"'])
    assert all(p.read_bytes() == raw for p, raw in originals.items())
    # Changing a delivered asset is caught; checks don't silently rebuild it.
    (tmp_path / "replay" / "app.js").write_text("bad")
    with pytest.raises(ValueError, match="checksum"):
        check(tmp_path / "replay")


def test_builder_rejects_original_directories_and_unallowlisted_outputs(tmp_path):
    for name in ["results", "scenarios", "mpa", "results/history/test"]:
        with pytest.raises(ValueError, match="protected"):
            build(ROOT / name)
    output = tmp_path / "replay"; output.mkdir(); (output / ".env").write_text("secret")
    with pytest.raises(ValueError, match="allowlist"):
        build(output)


@pytest.mark.parametrize("tamper", ["app.js", "recording.json", "bool_revision"])
def test_rehashing_tampered_artifact_cannot_authorize_its_contents(tmp_path, tamper):
    asset = "recording.json" if tamper == "bool_revision" else tamper
    output = tmp_path / "replay"
    build(output)
    path = output / asset
    if asset == "app.js":
        path.write_text("// forged application\n", encoding="utf-8")
    else:
        recording = json.loads(path.read_text(encoding="utf-8"))
        if tamper == "bool_revision":
            recording["frames"][0]["state"]["revision"] = False
        else:
            recording["frames"][1]["state"]["comparison"]["report"]["candidate"]["metrics"]["onTimeUnits"] = 999
        path.write_text(json.dumps(recording, ensure_ascii=False), encoding="utf-8")
    manifest_path = output / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["files"][asset] = {"bytes": path.stat().st_size, "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ValueError, match="current source|current-source computation"):
        check(output)
