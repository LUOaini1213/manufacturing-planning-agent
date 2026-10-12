"""Mutation check: compilable defects must cause real pytest test failures.

Run without arguments for the full clean baseline and mutation suite, or use --self-test
to check the harness in temporary toy projects without touching repository sources.
Collection/setup errors, missing reports, crashes and timeouts are infrastructure errors,
not mutation kills. Each subprocess gets a fresh bytecode location and every source write
is covered by restoration, including compilation and test-launch failures.
"""
import os
import subprocess
import sys
import tempfile
import time
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TESTS = str(ROOT / "tests")
TEST_TIMEOUT_S = 300
MUTANTS = [
    # Rolling state must preserve past work, machine ownership and human approval.
    ("mpa/rolling_state.py", "for event in events:", "for event in ():"),
    ("mpa/rolling_state.py", 'if not isinstance(events, list) or events[:len(snapshot["events"])] != snapshot["events"]:', "if False:"),
    ("mpa/rolling.py", "if confirmed is not True:", "if False:"),
    ("mpa/rolling_state.py", "simpy.Resource(env, capacity=1)", "simpy.Resource(env, capacity=2)"),
    ("mpa/rolling_state.py", "state = deepcopy(snapshot)", 'state = deepcopy(snapshot); state["executedPrefix"] = []'),
    ("mpa/rolling_state.py", 'snapshot.get("scenarioHash") != scenario_hash(scenario)', "False"),
    ("mpa/rolling.py", "if new_score <= old_score + 1e-8:", "if True:"),
    # Missing availability is silently zero-filled.
    ("mpa/scenario.py", "None if value is None else _number(value, f\"{prefix}.available[{index}]\")", "0.0 if value is None else _number(value, f\"{prefix}.available[{index}]\")"),
    # Boolean hours are treated as numeric.
    ("mpa/scenario.py", "if isinstance(value, bool) or not isinstance(value, (int, float)):", "if not isinstance(value, (int, float)):"),
    # Nonfinite numeric values bypass validation.
    ("mpa/scenario.py", "if not math.isfinite(result) or result < 0 or (positive and result == 0):", "if result < 0 or (positive and result == 0):"),
    # Negative costs and capacities bypass validation.
    ("mpa/scenario.py", "if not math.isfinite(result) or result < 0 or (positive and result == 0):", "if not math.isfinite(result) or (positive and result == 0):"),
    # Overlong period vectors are silently accepted.
    ("mpa/scenario.py", "if len(values) != length:", "if len(values) < length:"),
    # Overlapping regular-plus-overtime periods are allowed.
    ("mpa/scenario.py", "if span < minimum:", "if False:"),
    # Duplicate identifiers are allowed.
    ("mpa/scenario.py", "if len(set(values)) != len(values):", "if False:"),
    # Breakdown reduces capacity but leaves the outage beginning open.
    ("mpa/scenario.py", "machine[\"available_start\"][period] += lost", "machine[\"available_start\"][period] += 0.0"),
    # Breakdown duration is ignored.
    ("mpa/scenario.py", "lost = min(float(breakdown[\"hours\"]), available)", "lost = 0.0"),
    # Zero processing rates are accepted.
    ("mpa/scenario.py", "rates[machine_id] = _number(value, f\"{prefix}.hours.{stage}.{machine_id}\", positive=True)", "rates[machine_id] = _number(value, f\"{prefix}.hours.{stage}.{machine_id}\", positive=False)"),
    # Remove the two coordinated bounds enforcing a stopped machine.
    ("mpa/solve.py", "    for machine in range(n_m):\n        for period in range(n_t):\n            upper[overtime_index(machine, period)] = cap if machines[machine][\"available\"][period] > 0 else 0\n    for i, order in enumerate(orders):\n        upper[late_index(i)] = order[\"qty\"]\n        upper[unfinished_index(i)] = order[\"qty\"]\n        for s, stage in enumerate(stages):\n            for m, machine in enumerate(machines):\n                eligible = machine[\"id\"] in order[\"hours\"].get(stage, {})\n                for period in range(n_t):\n                    active = machine[\"available\"][period] > 0\n                    upper[x_index(i, s, m, period)] = order[\"qty\"] if eligible and active else 0", "    for machine in range(n_m):\n        for period in range(n_t):\n            upper[overtime_index(machine, period)] = cap\n    for i, order in enumerate(orders):\n        upper[late_index(i)] = order[\"qty\"]\n        upper[unfinished_index(i)] = order[\"qty\"]\n        for s, stage in enumerate(stages):\n            for m, machine in enumerate(machines):\n                eligible = machine[\"id\"] in order[\"hours\"].get(stage, {})\n                for period in range(n_t):\n                    active = machine[\"available\"][period] > 0\n                    upper[x_index(i, s, m, period)] = order[\"qty\"] if eligible else 0"),
    # Report free lateness slack instead of actual completion periods.
    ("mpa/solve.py", "late_units = sum(row[\"units\"] for row in completed if row[\"period\"] > order[\"due\"])", "late_units = int(round(result.x[late_index(i)]))"),
    # Round necessary overtime out of the executable plan.
    ("mpa/solve.py", "overtime.append({\"machine\": machine[\"id\"], \"period\": period, \"period_name\": period_name, \"hours\": hours})", "overtime.append({\"machine\": machine[\"id\"], \"period\": period, \"period_name\": period_name, \"hours\": round(hours, 4)})"),
    # Drop cumulative fabrication-before-test allocation precedence.
    ("mpa/solve.py", "add(precedence, 0, np.inf)", "add(precedence, -np.inf, np.inf)"),
    # Expose zero windows to the rule arithmetic tolerance.
    ("mpa/rules.py", "        for period, hours in enumerate(machine[\"available\"])\n        if hours > 0", "        for period, hours in enumerate(machine[\"available\"])"),
    # Disable legitimate overtime after a positive partial shift.
    ("mpa/rules.py", "(machine[\"id\"], period): float(scenario[\"overtime_cap_hours\"]) if machine[\"available\"][period] > 0 else 0.0", "(machine[\"id\"], period): 0.0"),
    # Round the rule overtime total away from its authorizations.
    ("mpa/rules.py", "overtime_hours = sum(item[\"hours\"] for item in overtime)", "overtime_hours = round(sum(item[\"hours\"] for item in overtime), 4)"),
    # Run stages past the closing window.
    ("mpa/simulate.py", "duration = min(remaining, window[\"end\"] - start)", "duration = remaining"),
    # Overlap machine calendars across periods.
    ("mpa/simulate.py", "start = period * span + machine[\"available_start\"][period]", "start = machine[\"available_start\"][period]"),
    # Ignore a delayed regular-window start.
    ("mpa/simulate.py", "start = period * span + machine[\"available_start\"][period]", "start = period * span"),
    # Permit two jobs to occupy one machine concurrently.
    ("mpa/simulate.py", "simpy.Resource(env, capacity=1)", "simpy.Resource(env, capacity=2)"),
    # Reverse enqueue order so later unit identities overtake earlier ones.
    ("mpa/simulate.py", "for fab, test in sorted(jobs, key=lambda pair: (pair[0][\"period\"], pair[0][\"order\"], pair[0][\"unit\"])):", "for fab, test in sorted(jobs, key=lambda pair: (pair[0][\"period\"], pair[0][\"order\"], pair[0][\"unit\"]), reverse=True):"),
    # Run test stages without completing fabrication.
    ("mpa/simulate.py", "fab_finish = yield from run_stage(fab)", "fab_finish = env.now"),
    # Accept an externally overallocated machine-period.
    ("mpa/simulate.py", "if hours > authorized + 1e-9:", "if False:"),
    # Accept overtime authorization beyond the cap.
    ("mpa/simulate.py", "if hours > scenario[\"overtime_cap_hours\"] + 1e-9:", "if False:"),
    # Accept external overtime for a fully stopped period.
    ("mpa/simulate.py", "if machines[row[\"machine\"]][\"available\"][period] == 0 and hours > 0:", "if False:"),
    # Charge actual overtime rather than the reserved authorization.
    ("mpa/simulate.py", "_accounting_cost(scenario, tardy, unfinished, scheduled)", "_accounting_cost(scenario, tardy, unfinished, actual)"),
    # Omit unassigned demand from unfinished units.
    ("mpa/simulate.py", "unfinished = sum(order[\"qty\"] for order in orders.values()) - len(finishes)", "unfinished = len(stage_jobs) // 2 - len(finishes)"),
    # Use the beginning instead of the end of the due period.
    ("mpa/simulate.py", "due_at = (orders[fab[\"order\"]][\"due\"] + 1) * span", "due_at = orders[fab[\"order\"]][\"due\"] * span"),
    # CLSP audit accepts negative production quantities.
    ("mpa/clsp.py", "and math.isfinite(value) and value >= 0)", "and math.isfinite(value))"),
    # CLSP audit accepts boolean numeric fields.
    ("mpa/clsp.py", "valid = (not isinstance(value, bool) and isinstance(value, Real)", "valid = (isinstance(value, Real)"),
    # CLSP production audit skips row dimensions and element validation.
    ("mpa/clsp.py", "_matrix_shape(production, n, periods, \"production\")", "_sequence(production, n, \"production\")"),
    # Tiny positive lots evade setup capacity and cost.
    ("mpa/clsp.py", "            qty = production[item][period]\n            if qty > 0:\n                load_terms.extend", "            qty = production[item][period]\n            if qty > 1e-6:\n                load_terms.extend"),
    # A fixed decimal tolerance hides real backlog and overload.
    ("mpa/clsp.py", "return 64 * math.ulp(max(abs(float(value)) for value in values))", "return 1e-4"),
    # Round continuous solver production to four decimals.
    ("mpa/clsp.py", "return [[float(values[index(item, period)]) for period in range(periods)] for item in range(n)]", "return [[round(float(values[index(item, period)]), 4) for period in range(periods)] for item in range(n)]"),
    # Trust the solver objective without independently auditing production.
    ("mpa/clsp.py", "    try:\n        checked = audit(data, production)\n        _nonnegative(result.fun, \"solver objective\")", "    try:\n        checked = {\"objective\": round(float(result.fun), 4)}\n        _nonnegative(result.fun, \"solver objective\")"),
    # Ignore trailing numeric Trigeiro records.
    ("mpa/clsp.py", "if len(rows) != 3 + n_items + n_periods:", "if len(rows) < 3 + n_items + n_periods:"),
    # Apply an unauthorized due-date relaxation while claiming denial.
    ("mpa/agent.py", "    if any(marker in request for marker in RELAX_MARKERS):\n        log.append(", "    if any(marker in request for marker in RELAX_MARKERS):\n        for order in scenario[\"orders\"]:\n            order[\"due\"] = len(scenario[\"periods\"]) - 1\n        log.append("),
    # Recommend a worse replay without comparison.
    ("mpa/agent.py", "accepted = new_score <= old_score + 1e-6", "accepted = True"),
    # Ignore unfinished demand in the replay policy score.
    ("mpa/agent.py", "+ replay[\"unfinished_units\"] * weights[\"unfinished\"]", "+ 0 * replay[\"unfinished_units\"] * weights[\"unfinished\"]"),
    # Apply the same outage twice during fixed-workflow replay.
    ("mpa/workflow.py", "checked = simulate(updated, plan, None)", "checked = simulate(updated, plan, breakdown)"),
    # Hide unfinished units in the public replay report.
    ("mpa/evaluate.py", "view[\"sim_unfinished_units\"] = sim.get(\"unfinished_units\")", "view[\"sim_unfinished_units\"] = 0"),
    # Omit product sources from reproduction provenance.
    ("mpa/reporting.py", "paths = sorted(set(inputs + list((ROOT / \"mpa\").glob(\"*.py\"))))", "paths = sorted(set(inputs))"),
    # Claim an optimal CLSP result while restoring the default nonzero MIP gap.
    ("mpa/clsp.py", 'options={"time_limit": 30.0, "mip_rel_gap": 0.0}', 'options={"time_limit": 30.0}'),
]

class HarnessError(RuntimeError):
    """The run supplies no valid evidence about a mutant."""


def replacement(root, mutant):
    fname, old, new = mutant
    path = root / fname
    raw = path.read_bytes()
    text = raw.decode("utf-8")
    # LF-authored anchors also work in Windows CRLF checkouts; restoration still uses raw bytes.
    newline = "\r\n" if "\r\n" in text else "\n"
    old = old.replace("\r\n", "\n").replace("\n", newline)
    new = new.replace("\r\n", "\n").replace("\n", newline)
    if text.count(old) != 1 or old == new:
        raise HarnessError(f"mutation target must occur once and change: {fname}: {old[:70]}")
    return path, raw, text.replace(old, new).encode("utf-8")


def compile_source(raw, path):
    # Compile without importing the module or creating a .pyc next to product sources.
    try:
        compile(raw, str(path), "exec")
    except (SyntaxError, ValueError) as exc:
        raise HarnessError(f"mutant does not compile; not a kill: {path}: {exc}") from exc


def run_tests(root, tests, output_dir, *, fail_fast=False, timeout=TEST_TIMEOUT_S):
    output_dir.mkdir(parents=True)
    report = output_dir / "pytest.xml"
    env = dict(os.environ)
    env.pop("PYTEST_ADDOPTS", None)  # always run the declared suite, not a shell's -k filter
    env["PYTHONPATH"] = os.pathsep.join(filter(None, [str(root / "src"), str(root), env.get("PYTHONPATH")]))
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["PYTHONPYCACHEPREFIX"] = str(output_dir / "pycache")
    cmd = [sys.executable, "-B", "-m", "pytest", "-q", "-p", "no:cacheprovider",
           f"--junitxml={report}", str(tests)]
    if fail_fast:
        cmd.append("-x")
    try:
        result = subprocess.run(cmd, cwd=root, env=env, capture_output=True, text=True,
                                encoding="utf-8", errors="replace", timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        raise HarnessError(f"pytest timed out after {timeout}s; not a kill") from exc
    except OSError as exc:
        raise HarnessError(f"pytest could not start; not a kill: {exc}") from exc
    output = result.stdout + result.stderr
    (output_dir / "pytest.log").write_text(output, encoding="utf-8")
    try:
        document = ET.parse(report)
    except (OSError, ET.ParseError) as exc:
        raise HarnessError(f"pytest exited {result.returncode} without a valid report; not a kill:\n"
                           + output[-3000:]) from exc
    cases = document.findall(".//testcase")
    errors = document.findall(".//error")
    skipped = sum(case.find("skipped") is not None for case in cases)
    failures = []
    for case in cases:
        failure = case.find("failure")
        if failure is not None:
            name = f"{case.get('classname', '')}::{case.get('name', '')}"
            message = " ".join((failure.get("message") or failure.text or "test failure").split())
            failures.append(f"{name}: {message[:180]}")
    if errors or not cases or len(cases) == skipped or result.returncode not in (0, 1):
        raise HarnessError(f"pytest exited {result.returncode}, {len(errors)} errors, "
                           f"{len(cases)} cases/{skipped} skipped; not a kill:\n" + output[-3000:])
    if (result.returncode == 1) != bool(failures):
        raise HarnessError(f"pytest exit/report disagree; not a kill:\n" + output[-3000:])
    return {"killed": bool(failures), "tests": len(cases), "skipped": skipped, "failures": failures}


def run_mutant(root, tests, mutant, output_dir, *, timeout=TEST_TIMEOUT_S):
    path, raw, changed = replacement(root, mutant)
    try:
        path.write_bytes(changed)
        compile_source(path.read_bytes(), path)
        return run_tests(root, tests, output_dir, fail_fast=True, timeout=timeout)
    finally:
        path.write_bytes(raw)
        if path.read_bytes() != raw:
            raise HarnessError(f"source restoration failed: {path}")


def main():
    # Check every anchor and the syntax of every proposed mutant before any source write.
    originals = {}
    for mutant in MUTANTS:
        path, raw, changed = replacement(ROOT, mutant)
        originals[path] = raw
        compile_source(changed, path)
    started = time.monotonic()
    escaped = 0
    try:
        with tempfile.TemporaryDirectory(prefix="mpa-mutation-") as scratch:
            output_dir = Path(scratch)
            base = run_tests(ROOT, TESTS, output_dir / "baseline")
            if base["killed"]:
                raise HarnessError("unmodified tests fail; fix the baseline first:\n" + "\n".join(base["failures"]))
            print(f"baseline: {base['tests']} cases, {base['skipped']} skipped, "
                  f"{time.monotonic() - started:.1f}s", flush=True)
            for i, mutant in enumerate(MUTANTS, 1):
                result = run_mutant(ROOT, TESTS, mutant, output_dir / f"mutant-{i:03d}")
                escaped += not result["killed"]
                evidence = result["failures"][0] if result["killed"] else "no test failed"
                anchor = " ".join(mutant[1].split())[:70]
                print(f"{'killed ' if result['killed'] else 'ESCAPED'} [{i}/{len(MUTANTS)}] "
                      f"{mutant[0]}: {anchor} -- {evidence}", flush=True)
                if not result["killed"]:
                    raise HarnessError(f"mutant {i}/{len(MUTANTS)} escaped; stopped for review after restoring source")
    finally:
        unexpected = [path for path, raw in originals.items() if not path.exists() or path.read_bytes() != raw]
        for path in unexpected:
            path.write_bytes(originals[path])
        if unexpected:
            raise HarnessError("unexpected source changes restored: " + ", ".join(map(str, unexpected)))
    print(f"{len(MUTANTS) - escaped}/{len(MUTANTS)} mutants killed "
          f"({time.monotonic() - started:.1f}s); all source bytes restored", flush=True)
    return 1 if escaped else 0


def self_test():
    """Exercise failure classification/restoration without importing product code."""
    import py_compile
    import unittest
    from unittest.mock import patch

    class HarnessTests(unittest.TestCase):
        def setUp(self):
            self.scratch = tempfile.TemporaryDirectory(prefix="mpa-harness-test-")
            self.addCleanup(self.scratch.cleanup)
            self.root = Path(self.scratch.name)
            self.source = self.root / "subject.py"
            self.original = b"VALUE = 1\n"
            self.source.write_bytes(self.original)
            self.tests = self.root / "test_subject.py"
            self.tests.write_text("from subject import VALUE\ndef test_value():\n    assert VALUE == 1\n")

        def mutate(self, new, **kwargs):
            try:
                return run_mutant(self.root, self.tests, ("subject.py", "VALUE = 1", new),
                                  self.root / "run", **kwargs)
            finally:
                self.assertEqual(self.source.read_bytes(), self.original)

        def test_assertion_failure_ignores_stale_bytecode(self):
            py_compile.compile(str(self.source), doraise=True,
                               invalidation_mode=py_compile.PycInvalidationMode.UNCHECKED_HASH)
            result = self.mutate("VALUE = 2")
            self.assertTrue(result["killed"])
            self.assertIn("test_value", result["failures"][0])

        def test_multiline_anchor_preserves_windows_line_endings(self):
            raw = b"VALUE = 1\r\nUNUSED = 0\r\n"
            self.source.write_bytes(raw)
            path, original, changed = replacement(self.root,
                ("subject.py", "VALUE = 1\nUNUSED = 0", "VALUE = 2\nUNUSED = 0"))
            self.assertEqual(original, raw)
            self.assertEqual(changed, b"VALUE = 2\r\nUNUSED = 0\r\n")
            self.assertEqual(path.read_bytes(), raw)

        def test_survivor_is_not_a_kill(self):
            self.assertFalse(self.mutate("VALUE = 1 # unchanged behavior")["killed"])

        def test_compile_failure_restores_source(self):
            with self.assertRaisesRegex(HarnessError, "does not compile"):
                self.mutate("VALUE = (")

        def test_collection_failure_is_not_a_kill(self):
            with self.assertRaisesRegex(HarnessError, "not a kill"):
                self.mutate("raise ImportError('synthetic collection failure')")

        def test_setup_failure_is_not_a_kill(self):
            self.tests.write_text("import pytest\n@pytest.fixture(autouse=True)\n"
                                  "def broken():\n    raise RuntimeError('synthetic fixture failure')\n"
                                  "def test_value():\n    assert False\n")
            with self.assertRaisesRegex(HarnessError, "not a kill"):
                self.mutate("VALUE = 2")

        def test_timeout_is_not_a_kill_and_restores_source(self):
            with self.assertRaisesRegex(HarnessError, "timed out"):
                self.mutate("import time; time.sleep(10); VALUE = 1", timeout=1)

        def test_launch_failure_is_not_a_kill_and_restores_source(self):
            with patch.object(subprocess, "run", side_effect=OSError("synthetic launch failure")):
                with self.assertRaisesRegex(HarnessError, "could not start; not a kill"):
                    self.mutate("VALUE = 2")

        def test_crash_without_report_is_not_a_kill(self):
            with self.assertRaisesRegex(HarnessError, "without a valid report; not a kill"):
                self.mutate("import os; os._exit(3)")

        def test_all_skipped_is_not_a_kill(self):
            self.tests.write_text("import pytest\n@pytest.mark.skip(reason='synthetic skip')\n"
                                  "def test_value():\n    assert False\n")
            with self.assertRaisesRegex(HarnessError, "not a kill"):
                self.mutate("VALUE = 2")

        def test_exit_and_failure_report_must_agree(self):
            def fake_run(*args, **kwargs):
                (self.root / "run" / "pytest.xml").write_text(
                    '<testsuites><testsuite><testcase name="fake">'
                    '<failure message="synthetic"/></testcase></testsuite></testsuites>')
                return subprocess.CompletedProcess(args[0], 0, stdout="", stderr="")

            with patch.object(subprocess, "run", side_effect=fake_run):
                with self.assertRaisesRegex(HarnessError, "exit/report disagree; not a kill"):
                    self.mutate("VALUE = 2")

    result = unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(HarnessTests))
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    if sys.argv[1:] == ["--self-test"]:
        sys.exit(self_test())
    if sys.argv[1:]:
        sys.exit("usage: python tests/mutate.py [--self-test]")
    try:
        sys.exit(main())
    except HarnessError as exc:
        sys.exit(f"MUTATION ERROR: {exc}")
