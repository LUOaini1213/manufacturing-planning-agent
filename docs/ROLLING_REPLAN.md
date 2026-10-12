# Event-triggered rolling planning

This separate deterministic mode resumes an actual execution snapshot. It does
not replace the existing aggregate-capacity benchmark or turn its historical
stochastic results into evidence about rolling dispatch.

```bash
python -m pip install -r requirements-dev.txt
python -m mpa.rolling --output-dir artifacts/rolling
# Open artifacts/rolling/index.html in a browser.
```

The CLI runs the committed original four-unit scenario through SimPy until hour
2, then introduces M1's outage `[2,6)`. A/0 has finished both stages. A/1 has
actually consumed 0.5 fabrication hours, with 1 hour remaining. It cannot be
rebuilt, forgotten or moved to M2. Only B's unstarted operations may change route.
Both alternatives then execute from the same snapshot with the outage applied:
the original routes finish 2 units and leave 2 unfinished; the future proposal
finishes all 4, without reserving overtime or changing due dates. This is one
authored deterministic example, not a measured industrial improvement rate.

## Snapshot and event protocol

`initial_snapshot(scenario, initial_plan, current_time)` physically runs SimPy to
the cutoff; it includes completions/start commitments exactly at that time and
clips any ongoing segment's processed hours. Every demanded unit has separate
fabrication/testing records, including work not allocated by the initial plan.
The snapshot stores `currentTime`, the executed prefix, each operation's
processed/remaining hours, state, machine, start and completion, original overtime
authorization, and known calendar events. Start commitments preserve an operation
that acquired ownership exactly at a cutoff even if it has consumed zero hours.
The commitment records which event history was known when it began.

`propose(scenario, snapshot, {machine, start, end}, policy=..., step=...)` accepts
only an outage starting at or after currentTime. The base scenario is immutable,
including due dates, rates, demand and calendars; a source-independent normalized
scenario hash binds the snapshot to it. Known events, past processing and start
commitments cannot be rewritten. The proposal compares the original remaining
routes and the candidate using the same policy and execution state. A failed or
worse candidate retains the original recommendation and exposes the reason.

`advance_confirmed(..., choice="candidate"|"original", confirmed=True)` is the only
adoption/advance entry point. It requires explicit confirmation and refuses a
proposal generated for another snapshot. It returns a new snapshot, enabling a
second later event while retaining the newly executed prefix. Proposing and
comparing do not mutate the caller's state or issue production commands.

## In-process semantics

An operation becomes committed when it first starts work. After that it retains
its original machine and ownership until completion. Outages and normal closure
pause processing; the next legal window resumes only its exact remaining hours.
Other jobs cannot overtake that WIP during a pause. Completed operations do not
re-enter queues, and testing starts only after that unit's fabrication completes.
WIP with no remaining capacity stays explicitly unfinished with its residual
work retained. Changing machine, scrapping/restarting a unit, splitting it among
machines and non-resumable processes are outside this mode's assumptions.

Processing times are known, deterministic nominal values. This mode does not
observe or forecast stochastic duration realizations. All original demand is
assumed released. There are no new-order releases, setup/changeover costs, stock
or workforce constraints, live MES ingestion or industrial validation.

## Solver, checks and explanation

HiGHS chooses the route and start for unstarted operations only. Future starts
lie on a configurable grid (0.5 hours in the example); processing durations
remain exact. Machine ownership intervals, including pauses, are conservatively
rasterized. This can exclude otherwise feasible continuous-time schedules; an
`optimal_on_conservative_grid` result is not global continuous-time optimality.
The prototype bounds work at 32 units, 256 ticks and 20,000 candidates, with a
15-second solver limit. A solver limit does not invent an accepted replacement.

The overtime authorization is fixed from the original plan. The candidate may
use different authorized hours but cannot open unapproved overtime or a machine
that has no legal window. Scores charge reserved overtime consistently in both
arms; actual consumption is reported separately. This mode optimizes completion
and lateness within that fixed authorization, rather than purchasing extra hours.

Before a placement is replayed, interval, eligibility, WIP ownership, processing
conservation and predecessor checks audit it. SimPy then executes its actual
work segments, and snapshot checks reconcile the trace with all demanded units.
Tests additionally calculate legal windows directly from the raw scenario and
verify independent small hand-calculated outcomes, so the calendar checks do
not merely compare a solver result against its own constraints.

`comparison.json` includes the before/after states, exact placements, policy
scores, source/input SHA256, commit, working-tree status, Python and dependency
versions. `index.html` also exposes the cutoff's completed/WIP/unstarted stage
counts and each WIP's frozen machine, processed and remaining hours.
`gantt.svg` and `index.html` show the shared gray executed prefix,
fabrication/testing suffixes, outage and cutoff; bars expose identities/times on
hover. They label the candidate pending human confirmation. Old comparison and
F1 reports remain untouched; the rolling CLI refuses their original/history
output directories. Use the separate rolling report when discussing these new
results, rather than attributing them to the old full-horizon study.
