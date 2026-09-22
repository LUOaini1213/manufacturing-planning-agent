import json
import unittest
from pathlib import Path

from mpa.agent import run as run_agent
from mpa.evaluate import evaluate
from mpa.rules import earliest_due_date
from mpa.scenario import load, normalize
from mpa.simulate import simulate
from mpa.solve import solve
from mpa.workflow import run as run_workflow

ROOT = Path(__file__).resolve().parents[1]
COMPARED = ("status", "policy", "on_time_units", "tardy_units", "unfinished_units", "accounting_cost")


class PlanningTest(unittest.TestCase):
    def setUp(self):
        self.surge = load(ROOT / "scenarios" / "surge_downtime.json")

    def test_solver_respects_downtime_and_precedence(self):
        plan = solve(self.surge, "cost_min")
        self.assertTrue(plan["ok"])
        self.assertEqual(plan["status"], "optimal")
        self.assertFalse(any(row["machine"] == "M2" and row["period"] == 1 for row in plan["assignment"]))
        for order in {row["order"] for row in plan["assignment"]}:
            fab = {}
            test = {}
            for row in plan["assignment"]:
                if row["order"] != order:
                    continue
                bucket = fab if row["stage"] == "fab" else test
                bucket[row["period"]] = bucket.get(row["period"], 0) + row["units"]
            fab_cum = test_cum = 0
            for period in range(2):
                fab_cum += fab.get(period, 0)
                test_cum += test.get(period, 0)
                self.assertGreaterEqual(fab_cum, test_cum)
        orders = {item["id"]: item for item in self.surge["orders"]}
        available = {machine["id"]: machine["available"] for machine in self.surge["machines"]}
        for row in plan["assignment"]:
            self.assertIn(row["machine"], orders[row["order"]]["hours"][row["stage"]])
            self.assertGreater(available[row["machine"]][row["period"]], 0)

    def test_missing_hours_are_not_invented_by_the_agent(self):
        scenario = load(ROOT / "scenarios" / "missing_downtime.json")
        agent = run_agent(scenario)
        workflow = run_workflow(scenario)
        self.assertFalse(agent["plan"]["ok"])
        self.assertEqual(agent["plan"]["status"], "abstain")
        self.assertTrue(workflow["plan"]["ok"])
        assumed = next(item["assumed"] for item in workflow["log"] if item["tool"] == "fill_missing")
        self.assertIn("M2 D2 assumed 8h", assumed)
        self.assertEqual(workflow["plan"]["status"], "optimal")

    def test_due_date_relax_is_denied_and_the_model_is_unchanged(self):
        scenario = load(ROOT / "scenarios" / "contradictory_relax.json")
        agent = run_agent(scenario)
        denied = next(item for item in agent["log"] if item["tool"] == "relax_due_dates")
        first = next(item for item in agent["log"] if item["tool"] == "solve")
        self.assertEqual(denied["status"], "denied")
        self.assertTrue(agent["needs_human_confirm"])
        self.assertEqual(agent["model_calls"], 0)
        honest = solve(self.surge, first["policy"])
        self.assertEqual(first["accounting_cost"], honest["accounting_cost"])

    def test_optimizer_is_not_worse_than_the_rule_on_the_same_cost(self):
        rule = earliest_due_date(self.surge)
        optimal = solve(self.surge, "cost_min")
        self.assertTrue(rule["ok"])
        self.assertLessEqual(optimal["accounting_cost"], rule["accounting_cost"] + 1e-6)

    def test_highs_matches_exhaustive_enumeration(self):
        scenario = _reduced_instance()
        plan = solve(scenario, "cost_min")
        self.assertTrue(plan["ok"])
        self.assertEqual(plan["status"], "optimal")
        self.assertAlmostEqual(plan["objective"], _enumerate_best(scenario), places=4)
        self.assertFalse(any(row["machine"] == "M2" for row in plan["assignment"]))

    def test_nominal_replay_matches_plan_and_downtime_finishes_later(self):
        plan = solve(self.surge, "cost_min")
        nominal = simulate(self.surge, plan, None, pace=1)
        self.assertEqual(nominal["on_time_units"], plan["on_time_units"])
        self.assertEqual(nominal["tardy_units"], plan["tardy_units"])
        self.assertEqual(nominal["unfinished_units"], plan["unfinished_units"])
        delayed = simulate(self.surge, plan, self.surge["sim_breakdown"], pace=1)
        self.assertGreater(delayed["makespan"], nominal["makespan"])

    def test_seeded_simulation_can_miss_more_and_replan_once(self):
        plan = solve(self.surge, "due_first")
        sim = simulate(self.surge, plan, self.surge["sim_breakdown"])
        self.assertLess(sim["on_time_units"], plan["on_time_units"])
        agent = run_agent(self.surge)
        replans = [item for item in agent["log"] if item["tool"] == "replan" and "skipped" not in item]
        self.assertEqual(len(replans), 1)
        self.assertEqual(replans[0]["change"], "M1 period 1 8h -> 5h")
        self.assertEqual(agent["model_calls"], 0)
        self.assertTrue(agent["needs_human_confirm"])
        self.assertEqual(agent["log"][-1]["status"], "pending_human_confirm")

        slack = _slack_instance()
        slack_plan = solve(slack, "cost_min")
        slack_sim = simulate(slack, slack_plan, None)
        self.assertGreaterEqual(slack_sim["on_time_units"], slack_plan["on_time_units"])
        slack_agent = run_agent(slack)
        self.assertTrue(all("skipped" in item for item in slack_agent["log"] if item["tool"] == "replan"))

    def test_evaluation_records_three_arms_and_matches_readme(self):
        first = evaluate()
        second = evaluate()
        self.assertEqual(_signatures(first), _signatures(second))
        committed = json.loads((ROOT / "results" / "comparison.json").read_text(encoding="utf-8"))
        self.assertEqual(_signatures(first), _signatures(committed))
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        for scenario in first["scenarios"]:
            for arm in ("rule", "fixed_workflow", "agent"):
                self.assertIn(_cell(scenario[arm]), readme)
        surge = next(item for item in first["scenarios"] if item["name"] == "surge_downtime")
        workflow_log = surge["fixed_workflow"]["log"]
        self.assertIn("固定流程第一次带停机回放" + _logged_sim(workflow_log, "first_plan"), readme)
        self.assertIn("不再重复施加这次停机，回放" + _logged_sim(workflow_log, "replanned"), readme)
        agent_outage = next(item for item in surge["agent"]["log"] if item["tool"] == "simulate" and "which" not in item)
        self.assertIn("Agent 带停机回放" + _logged_sim_item(agent_outage), readme)
        self.assertNotIn("两条优化计划的仿真都是", readme)
        missing = next(item for item in first["scenarios"] if item["name"] == "missing_downtime")
        self.assertIn(_sim_cell(missing["fixed_workflow"]), readme)
        self.assertEqual(surge["agent"]["model_calls"], 0)
        self.assertTrue(surge["agent"]["needs_human_confirm"])
        for arm in ("rule", "fixed_workflow", "agent"):
            for key in ("on_time_units", "tardy_units", "unfinished_units", "overtime_hours", "accounting_cost"):
                self.assertIsInstance(surge[arm][key], (int, float))


def _signatures(report):
    found = {}
    for scenario in report["scenarios"]:
        found[scenario["name"]] = {arm: tuple(scenario[arm][key] for key in COMPARED) for arm in ("rule", "fixed_workflow", "agent")}
    return found


def _sim_cell(row):
    return f"准时 {_num(row['sim_on_time_units'])}、延期 {_num(row['sim_tardy_units'])}"


def _logged_sim(log, which):
    item = next(entry for entry in log if entry.get("tool") == "simulate" and entry.get("which") == which)
    return _logged_sim_item(item)


def _logged_sim_item(item):
    return f"准时 {_num(item['on_time_units'])}、延期 {_num(item['tardy_units'])}"


def _cell(row):
    if row["on_time_units"] is None:
        return "无方案"
    return (
        f"准时 {_num(row['on_time_units'])}，延期 {_num(row['tardy_units'])}，"
        f"未完工 {_num(row['unfinished_units'])}，加班 {_num(row['overtime_hours'])}，成本 {_num(row['accounting_cost'])}"
    )


def _num(value):
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def _reduced_instance():
    return {
        "periods": ["D1"],
        "hours_per_period": 8,
        "overtime_cap_hours": 2,
        "costs": {"overtime_per_hour": 10, "tardiness_per_unit": 5, "unfinished_per_unit": 20},
        "policies": {
            "cost_min": {"overtime": 10, "tardiness": 5, "unfinished": 20},
            "due_first": {"overtime": 10, "tardiness": 50, "unfinished": 200},
        },
        "stages": ["fab", "test"],
        "machines": [
            {"id": "M1", "available": [1.0]},
            {"id": "M2", "available": [0.0]},
        ],
        "orders": [
            {"id": "A", "qty": 2, "due": 0, "hours": {"fab": {"M1": 1.0}, "test": {"M1": 1.0}}},
        ],
    }


def _enumerate_best(scenario):
    """Independent integer search. Overtime is the least feasible hours above availability."""
    order = scenario["orders"][0]
    machines = scenario["machines"]
    weights = scenario["policies"]["cost_min"]
    cap = float(scenario["overtime_cap_hours"])
    qty = int(order["qty"])
    best = None
    for fab_1 in range(qty + 1):
        for fab_2 in range(qty + 1):
            for test_1 in range(qty + 1):
                for test_2 in range(qty + 1):
                    counts = {"M1": {"fab": fab_1, "test": test_1}, "M2": {"fab": fab_2, "test": test_2}}
                    if fab_1 + fab_2 != test_1 + test_2:
                        continue
                    if test_1 + test_2 > fab_1 + fab_2:
                        continue
                    unfinished = qty - (test_1 + test_2)
                    overtime = 0.0
                    feasible = True
                    for machine in machines:
                        hours = 0.0
                        for stage in ("fab", "test"):
                            rate = order["hours"].get(stage, {}).get(machine["id"])
                            units = counts[machine["id"]][stage]
                            if units and rate is None:
                                feasible = False
                            if rate:
                                hours += units * float(rate)
                        available = float(machine["available"][0])
                        extra = max(0.0, hours - available)
                        if extra > cap + 1e-9:
                            feasible = False
                        overtime += extra
                    if not feasible:
                        continue
                    objective = overtime * weights["overtime"] + unfinished * weights["unfinished"]
                    best = objective if best is None else min(best, objective)
    if best is None:
        raise AssertionError("reduced instance has no feasible assignment")
    return round(best, 4)


def _slack_instance():
    return normalize(
        {
            "periods": ["D1"],
            "hours_per_period": 8,
            "overtime_cap_hours": 2,
            "costs": {"overtime_per_hour": 80, "tardiness_per_unit": 50, "unfinished_per_unit": 200},
            "policies": {
                "cost_min": {"overtime": 80, "tardiness": 50, "unfinished": 200},
                "due_first": {"overtime": 80, "tardiness": 500, "unfinished": 2000},
            },
            "stages": ["fab", "test"],
            "machines": [
                {"id": "M1", "available": [8]},
                {"id": "M3", "available": [8]},
            ],
            "orders": [
                {"id": "A", "qty": 1, "due": 0, "hours": {"fab": {"M1": 1.0}, "test": {"M3": 1.0}}},
            ],
            "sim_seed": 1,
        }
    )


if __name__ == "__main__":
    unittest.main()
