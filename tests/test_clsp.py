import json
import unittest
from pathlib import Path

from mpa.clsp import audit, coordinate_plan, load_trigeiro, lot_for_lot, merge_earlier, shift_earlier, solve_clsp

ROOT = Path(__file__).resolve().parents[1]


class TrigeiroTest(unittest.TestCase):
    def test_f1_highs_beats_a_capacity_blind_rule(self):
        data = load_trigeiro()
        self.assertEqual(data["n_items"], 6)
        self.assertEqual(data["n_periods"], 15)
        self.assertEqual(data["capacity"], 728)
        optimal = solve_clsp(data)
        self.assertTrue(optimal["ok"])
        self.assertEqual(optimal["status"], "optimal")
        checked = audit(data, optimal["production"])
        self.assertAlmostEqual(checked["objective"], optimal["objective"], places=2)
        direct = lot_for_lot(data)
        self.assertFalse(direct["ok"])
        self.assertIn("728", direct["reason"])
        shifted = shift_earlier(data)
        if shifted["ok"]:
            self.assertGreaterEqual(shifted["objective"], optimal["objective"] - 1e-4)
        else:
            self.assertEqual(shifted["status"], "infeasible")
        merged = merge_earlier(data)
        self.assertTrue(merged["ok"])
        checked_merge = audit(data, merged["production"])
        self.assertAlmostEqual(checked_merge["objective"], merged["objective"], places=2)
        self.assertGreaterEqual(merged["objective"], optimal["objective"] - 1e-4)
        planned = coordinate_plan(data)
        self.assertTrue(planned["ok"])
        checked_plan = audit(data, planned["production"])
        self.assertAlmostEqual(checked_plan["objective"], planned["objective"], places=2)
        self.assertGreaterEqual(planned["objective"], optimal["objective"] - 1e-4)
        self.assertLess(planned["objective"], merged["objective"])
        saved = json.loads((ROOT / "results" / "trigeiro_f1.json").read_text(encoding="utf-8"))
        self.assertEqual(saved["highs"]["objective"], optimal["objective"])
        self.assertEqual(saved["highs"]["setups"], optimal["setups"])
        self.assertEqual(saved["merge_earlier"]["objective"], merged["objective"])
        self.assertEqual(saved["merge_earlier"]["setups"], merged["setups"])
        self.assertEqual(saved["coordinate_plan"]["objective"], planned["objective"])
        self.assertEqual(saved["coordinate_plan"]["setups"], planned["setups"])
        self.assertEqual(saved["lot_for_lot"]["reason"], direct["reason"])
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn(str(int(optimal["objective"])), readme)
        self.assertIn(str(int(merged["objective"])), readme)
        self.assertIn(str(int(planned["objective"])), readme)
        self.assertIn(f"准备 {optimal['setups']} 次", readme)
        self.assertIn(f"准备 {merged['setups']} 次", readme)
        self.assertIn(f"准备 {planned['setups']} 次", readme)
        self.assertIn("748", direct["reason"])
        self.assertIn("748", readme)
        self.assertIn("period 10", readme)


if __name__ == "__main__":
    unittest.main()
