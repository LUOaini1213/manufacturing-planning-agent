import unittest
from pathlib import Path

from mpa.agent import run as run_agent
from mpa.rules import earliest_due_date
from mpa.scenario import load
from mpa.solve import solve
from mpa.workflow import run as run_workflow

ROOT = Path(__file__).resolve().parents[1]


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

    def test_missing_hours_are_not_invented_by_the_agent(self):
        scenario = load(ROOT / "scenarios" / "missing_downtime.json")
        agent = run_agent(scenario)
        workflow = run_workflow(scenario)
        self.assertFalse(agent["plan"]["ok"])
        self.assertEqual(agent["plan"]["status"], "abstain")
        self.assertTrue(workflow["plan"]["ok"])
        self.assertTrue(any(item["tool"] == "fill_missing" and item["assumed"] for item in workflow["log"]))

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


if __name__ == "__main__":
    unittest.main()
