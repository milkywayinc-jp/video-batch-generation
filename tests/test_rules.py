import copy
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from videobatch.common import BatchError
from videobatch.rules import Timebase, resolve_plan, validate_rules
from videobatch.sheets import analyze_sheet, require_plan_unchanged, make_batches
from test_sheets import fixture, put
from support import interpretation, resolved


class RulesTests(unittest.TestCase):
    def test_unknown_uninterpreted_and_unattributed_rules_stop(self):
        snap = fixture()
        plan = analyze_sheet(snap, snap["sheets"][0])
        draft = interpretation(plan)
        draft["interpretations"].pop("H2")
        with self.assertRaisesRegex(BatchError, "Interpret every"):
            resolve_plan(plan, draft)
        draft = interpretation(plan)
        draft["values"]["video"]["typo"] = True
        with self.assertRaisesRegex(BatchError, "unknown/missing"):
            resolve_plan(plan, draft)
        draft = interpretation(plan)
        draft["provenance"].pop("values.video.width")
        with self.assertRaisesRegex(BatchError, "provenance"):
            resolve_plan(plan, draft)

    def test_new_sheet_rule_invalidates_all_old_execution_settings(self):
        snap = fixture()
        plan = resolved(analyze_sheet(snap, snap["sheets"][0]))
        changed = copy.deepcopy(snap)
        put(changed, 1, 0, {"stringValue": "縦長、24fps、末尾なし"})
        with self.assertRaisesRegex(BatchError, "changed"):
            require_plan_unchanged(plan, changed["sheets"][0])
        new_raw = analyze_sheet(changed, changed["sheets"][0])
        old = {k: v for k, v in plan["resolved_rules"].items()
               if k not in ("integrity", "source_plan_hash", "schema_version")}
        # Resolve again, never silently retain a previous execution manifest.
        new = resolved(new_raw, width=240, height=426, fps="24", tail=0, speed=1.4)
        self.assertNotEqual(plan["resolved_rules"]["integrity"], new["resolved_rules"]["integrity"])
        self.assertEqual(new["resolved_rules"]["values"]["video"]["height"], 426)
        self.assertEqual(new["batches"][0]["audio_rules"]["speed"], 1.4)
        new["resolved_rules"]["values"]["video"]["width"] = 500
        with self.assertRaisesRegex(BatchError, "changed"):
            validate_rules(new)

    def test_rational_frame_boundaries_do_not_accumulate_rounding_error(self):
        tb = Timebase("30000/1001")
        cursor = sum(tb.samples(i, 1) for i in range(100000))
        self.assertEqual(cursor, tb.sample(100000))
        self.assertEqual(tb.sample(100000), 160160000)
        self.assertEqual(Timebase("24").sample(24), 48000)
        self.assertEqual(Timebase("30").frames(0), 0)

    def test_batch_limit_and_speed_come_from_rules(self):
        snap = fixture()
        put(snap, 4, 8, {"stringValue": "文" * 20})
        put(snap, 5, 9, {})
        put(snap, 5, 8, {"stringValue": "文" * 20})
        raw = analyze_sheet(snap, snap["sheets"][0])
        small = resolved(raw, limit=30)
        large = resolved(raw, limit=100)
        self.assertGreater(len(small["batches"]), len(large["batches"]))


if __name__ == "__main__":
    unittest.main()
