# Browser outage exercise

The local page wraps the existing deterministic rolling engine. HiGHS makes a
proposal; SimPy physically resumes confirmed work. No model, API key, file upload,
factory ingestion or live production command is used. This is a four-unit,
two-stage authored scenario, with resumable work and the same conservative
0.5-hour grid, due dates, legal windows and overtime authorizations as the core.
It does not demonstrate industrial savings or LLM performance.

## Run and inspect

Install `requirements.txt`, then run `python -m mpa.demo --port 8840`. Open
`http://127.0.0.1:8840`. The exercise starts at hour 2, with A/0 completed and
A/1's fabrication committed on M1 (0.5 h processed, 1 h remaining).

1. Compare M1's outage `[2,6)`. The original suffix finishes 2 units and leaves
   2 unfinished; the candidate finishes 4, with no overtime or due-date changes.
2. Choose candidate, explicitly confirm, and advance to hour 3.5. Only the work
   actually executed through that time becomes the new immutable prefix.
3. Compare M2's outage `[3.5,5.5)`, then confirm and advance to hour 8. Its WIP
   retains M2; it cannot be moved or restarted. All four units finish.
4. Download exact evidence: current execution, normalized fixed input, pending
   or last comparison, every confirmed report and before/after snapshot hashes,
   exact placements/trace, and engine/API/frontend/input source fingerprints.

An invalid event does not alter the execution or pending comparison. A valid
new comparison replaces an earlier proposal and has its own revision/ID. A
missing, failed or worse candidate cannot be confirmed; original routes remain
available. A failed HTTP response is reconciled with authoritative state before
another mutation. If both response and reconciliation are unavailable, the
page blocks actions and asks for a state refresh, without pretending execution
has advanced or has been rolled back.

## Sessions, confirmation and transport

The process is loopback-only. A bounded JSON body, exact local Host/Origin,
request token and named machine/time inputs are required for mutations. Static
assets use a fixed allowlist, no arbitrary paths or user-provided scenario.
No CORS access is granted. This limits local browser mistakes/CSRF and DNS
rebinding; it is not multi-user authentication or a production hosting design.

The local server runs each HiGHS comparison in a fresh `spawn` child process,
single concurrent solve, with a 25-second budget including startup, result
transfer and exit (plus at most two seconds of terminate/kill cleanup). Another
session is rejected as temporarily busy rather than queued behind many solves.
A failed child, closed pipe, nonzero exit or timeout publishes no proposal and
cannot advance execution. Its source fingerprint must match the host snapshot.
This isolates the HTTP host from native solver failures; it does not modify the
solver/core or establish general native-library thread safety.
Stdin transfer is supervised separately because Windows' synchronous stdin
write can otherwise precede `communicate`'s timeout. Incomplete reader/writer
cleanup quarantines that worker and blocks future solves until a server restart;
pipe close is attempted only after readers finish. Stderr is discarded, not
shown to users. The 1 MB stdout check is an acceptance check after transfer, not
a streaming memory quota; the trusted fixed scenario produces a small report.

Each page owns an independent in-memory exercise. `sessionStorage` stores only
its resume credentials; a reload forks the latest saved state into fresh page
credentials. Duplicate tabs therefore start from the same snapshot but never
share subsequent decisions. These transport values are excluded from evidence
and public recordings. Requests are serialized per exercise, with a revision
check covering the solve and commit. Busy sessions reject conflicting reads or
actions immediately rather than queueing them; resume/fork does not hold the
global registry lock while an earlier action is running. A proposal can be adopted once, against the
snapshot for which it was produced, after explicit confirmation. Confirmation
can advance only to a strictly later time within the 10-hour horizon.

Idle exercises expire after 30 minutes; only the 24 most recently used exercises
are retained. Pending proposals expire after five minutes. Each exercise is
bounded to eight confirmed events; reset starts the original scenario again and
invalidates old proposals. Server restart or eviction is explicitly reported as
an unavailable previous exercise on reload; downloaded JSON remains evidence,
not an arbitrary-input restore interface.

Routes (all local, no private path/query input):

| Route | Contract |
|---|---|
| `POST /api/session` | Open or fork a previous page's state; fresh credentials |
| `GET /api/state` | Current authoritative revision, execution and comparison |
| `POST /api/propose` | Exact revision + `{machine,start,end}`; execution unchanged |
| `POST /api/advance` | Exact revision/proposal ID + choice + `confirmed:true` + until |
| `POST /api/reset` | Exact revision; invalidate prior exercise/proposal |
| `GET /api/export` | JSON evidence without credentials or machine-local paths |

## Public recorded replay

`python scripts/build_demo.py` computes all five frames using the same real
`DemoSession`/HiGHS/SimPy calls: snapshot, first comparison, first confirmation,
second comparison, second confirmation. This creates `build/pages`, separate
from the immutable `scenarios/`, `results/`, history and earlier metrics.
`python scripts/build_demo.py --check` checks the exact six-file allowlist,
artifact hashes, current source fingerprint and recorded-only interface. It also
compares every fixed asset with the current template and recomputes the recording
with the real current engine/input; forging values and rehashing a manifest does
not make the artifact valid. Only the recorded generation timestamp is reused.
The manifest includes normalized source/input hashes and the source commit.
No source tree, `.env`, dependency environment or private input is published.

The published page visibly says **Recorded replay · no live solver**. Next and
previous step through saved computations. Event inputs and confirmation are
disabled; downloads contain the chosen frame's exact evidence. To change the
event, run the local command above. Static Pages cannot execute Python.

The Pages workflow builds/checks on affected pull requests and uploads the
reviewed artifact. Deployment is intentionally skipped for PRs. Only manual
`workflow_dispatch` on the repository's actual default branch `main` can deploy;
this is separate from CI success and requires post-deployment URL/browser QA.

Tests use real loopback HTTP, solver and simulation for two events, independent
duplicate-tab forks, racing same-revision requests, one-time confirmation,
finite/bounded input, Host/Origin/token admission and export privacy. Controlled
solver-limit/worse-result cases verify that only original routes can be adopted.
Client fixtures test lost responses, unknown outcomes, refresh serialization and
malformed successful payloads. None of these are live-model evaluations.
