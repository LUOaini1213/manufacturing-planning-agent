"""Private stdio worker for the fixed local demo; no HTTP or filesystem input."""
import json
import sys

from mpa.demo import exact_fields, source_provenance, strict_json
from mpa.rolling import propose


def main():
    try:
        raw = sys.stdin.buffer.read(256_001)
        if len(raw) > 256_000:
            raise ValueError("worker input limit")
        body = strict_json(raw)
        exact_fields(body, {"scenario", "snapshot", "event", "step"})
        source = source_provenance()["sourceHash"]
        result = propose(body["scenario"], body["snapshot"], body["event"], step=body["step"])
        sys.stdout.write(json.dumps({"sourceHash": source, "report": result}, allow_nan=False))
        return 0
    except Exception:
        sys.stdout.write('{"failed":true}')
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
