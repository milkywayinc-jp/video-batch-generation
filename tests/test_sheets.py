import copy
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from videobatch.common import BatchError
from videobatch.sheets import (analyze_sheet, expected_writes, make_batches,
                               sheet_fingerprint, restore_entered, sheet_lock)
import tempfile
from unittest.mock import patch
from videobatch.common import save_json, file_hash


def fixture():
    rows = [["全体設定"], ["通常設定", "", "", "注釈設定", "注釈設定", "素材設定", "", "音声設定"],
            ["テロップ", "", "テロップスタイル個別指定", "注釈①", "注釈②", "素材名", "カット別指定", "秒数", "音声生成用テキスト", "音声個別指定", "BGM"],
            ["", "最初\nのテロップ", "", "", "提供：サンプル", "", "", "", "最初の音声"],
            ["", "素材音声", "", "", "", "", "", "", "素材にあるセリフ", "動画の音声をそのまま使用"],
            ["", "最後", "", "", "", "", "", "", "最後の音声"],
            [{"formulaValue": "=LEN(B7)"}], [{"formulaValue": "=LEN(B8)"}]]
    sheet = {"properties": {"sheetId": 123, "title": "★sample", "gridProperties": {"rowCount": 8, "columnCount": 11}},
             "merges": [{"sheetId": 123, "startRowIndex": 2, "endRowIndex": 3, "startColumnIndex": 0, "endColumnIndex": 2},
                        {"sheetId": 123, "startRowIndex": 3, "endRowIndex": 8, "startColumnIndex": 4, "endColumnIndex": 5}],
             "data": [{"rowData": [{"values": [{"userEnteredValue": x if isinstance(x, dict) else {"stringValue": x}}
                                              if x else {} for x in row]} for row in rows]}]}
    return {"spreadsheetId": "synthetic-test-only", "sheets": [sheet]}


def put(snapshot, row, col, val):
    rows = snapshot["sheets"][0]["data"][0]["rowData"]
    while len(rows) < row:
        rows.append({"values": []})
    values = rows[row - 1]["values"]
    while len(values) <= col:
        values.append({})
    values[col] = {"userEnteredValue": val}


class SheetsTests(unittest.TestCase):
    def test_merged_header_len_and_global_note_are_not_cuts(self):
        snap = fixture()
        plan = analyze_sheet(snap, snap["sheets"][0])
        self.assertEqual([c["row"] for c in plan["cuts"]], [4, 5, 6])
        self.assertEqual(plan["total_row"], 7)
        self.assertTrue(all(c["note2"] == "提供：サンプル" for c in plan["cuts"]))
        self.assertEqual(plan["cuts"][1]["audio_mode"], "source")
        self.assertEqual([b["rows"] for b in plan["batches"]], [[4], [6]])
        self.assertEqual(plan["cuts"][0]["caption"], "最初\nのテロップ")

    def test_gaps_and_unowned_total(self):
        snap = fixture()
        put(snap, 7, 6, {"stringValue": "合計秒数"})
        put(snap, 7, 7, {"numberValue": 11})
        plan = analyze_sheet(snap, snap["sheets"][0])
        timing = {"source_hash": plan["source_hash"], "cuts": [{"row": r, "frames": 30} for r in [4, 5, 6]], "total_frames": 90}
        with self.assertRaisesRegex(BatchError, "unowned"):
            expected_writes(plan, timing, snap)
        put(snap, 8, 1, {"stringValue": "追加"})
        self.assertEqual(analyze_sheet(snap, snap["sheets"][0])["last_cut_row"], 8)

    def test_concurrent_edit_refuses_all_writes(self):
        snap = fixture()
        plan = analyze_sheet(snap, snap["sheets"][0])
        timing = {"source_hash": plan["source_hash"], "cuts": [{"row": r, "frames": 31} for r in [4, 5, 6]], "total_frames": 93}
        changed = copy.deepcopy(snap)
        put(changed, 5, 8, {"stringValue": "同時編集"})
        with self.assertRaisesRegex(BatchError, "Sheet changed"):
            expected_writes(plan, timing, changed)
        writes = expected_writes(plan, timing, snap)
        self.assertEqual(writes["7:7"], {"numberValue": 3.1})
        self.assertTrue(all(k.endswith(":7") or k == "7:6" for k in writes))

    def test_old_total_moves_without_clearing_new_cut_duration(self):
        snap = fixture()
        put(snap, 7, 6, {"stringValue": "合計秒数"})
        put(snap, 7, 7, {"numberValue": 3})
        put(snap, 7, 1, {"stringValue": "新カット"})
        plan = analyze_sheet(snap, snap["sheets"][0])
        previous = {"status": "confirmed", "total_row": 7,
                    "writes": {"7:6": {"stringValue": "合計秒数"}, "7:7": {"numberValue": 3}}}
        timing = {"source_hash": plan["source_hash"], "cuts": [{"row": r, "frames": 30} for r in [4, 5, 6, 7]], "total_frames": 120}
        writes = expected_writes(plan, timing, snap, previous)
        self.assertEqual(writes["7:6"], {})
        self.assertEqual(writes["7:7"], {"numberValue": 1})
        self.assertEqual(writes["8:7"], {"numberValue": 4})

    def test_last_row_at_grid_limit_stops(self):
        snap = fixture()
        put(snap, 8, 1, {"stringValue": "最後"})
        plan = analyze_sheet(snap, snap["sheets"][0])
        timing = {"source_hash": plan["source_hash"], "cuts": [{"row": r, "frames": 30} for r in [4, 5, 6, 8]], "total_frames": 120}
        with self.assertRaisesRegex(BatchError, "blank row"):
            expected_writes(plan, timing, snap)

    def test_batches_account_for_newlines_and_utf16(self):
        cuts = [{"row": r, "audio_mode": "narration", "speech": text}
                for r, text in [(4, "あ" * 70), (5, "い" * 69), (6, "う")]]
        batches = make_batches(cuts)
        self.assertEqual([b["rows"] for b in batches], [[4, 5], [6]])
        self.assertEqual(batches[0]["offsets"][1]["start"], 71)
        with self.assertRaises(BatchError):
            make_batches([{"row": 4, "audio_mode": "narration", "speech": "😀" * 71}])

    def test_unowned_old_total_not_at_destination_blocks_write(self):
        snap = fixture()
        put(snap, 7, 6, {"stringValue": "合計秒数"})
        put(snap, 7, 7, {"numberValue": 3})
        put(snap, 8, 1, {"stringValue": "後から増えたカット"})
        snap["sheets"][0]["properties"]["gridProperties"]["rowCount"] = 9
        plan = analyze_sheet(snap, snap["sheets"][0])
        timing = {"source_hash": plan["source_hash"], "cuts": [{"row": r, "frames": 30} for r in [4, 5, 6, 8]], "total_frames": 120}
        with self.assertRaisesRegex(BatchError, "unowned old total"):
            expected_writes(plan, timing, snap)

    def test_restore_cells_omitted_by_sheets_after_a_clear(self):
        snap = fixture()
        before = sheet_fingerprint(snap["sheets"][0])
        put(snap, 4, 7, {"numberValue": 2})
        restore_entered(snap["sheets"][0], {"4:7": {}})
        self.assertEqual(sheet_fingerprint(snap["sheets"][0]), before)
        restore_entered(snap["sheets"][0], {"8:6": {"stringValue": "合計秒数"}})
        from videobatch.sheets import entered, entered_cells
        self.assertEqual(entered(entered_cells(snap["sheets"][0]), 8, 6), {"stringValue": "合計秒数"})

    def test_different_state_directories_share_same_host_lock(self):
        plan = {"spreadsheet_id": "synthetic-lock-test", "sheet_id": 987}
        with tempfile.TemporaryDirectory() as directory:
            with sheet_lock(Path(directory) / "first", plan):
                with self.assertRaisesRegex(BatchError, "Another local run"):
                    with sheet_lock(Path(directory) / "second", plan):
                        self.fail("Concurrent lock succeeded")

    def test_unmerged_bgm_alone_is_an_orphan(self):
        snap = fixture()
        put(snap, 8, 10, {"stringValue": "track.wav"})
        with self.assertRaisesRegex(BatchError, "Orphan"):
            analyze_sheet(snap, snap["sheets"][0])

    def test_boolean_frame_count_is_invalid(self):
        snap = fixture()
        plan = analyze_sheet(snap, snap["sheets"][0])
        timing = {"source_hash": plan["source_hash"], "cuts": [{"row": r, "frames": True} for r in [4, 5, 6]], "total_frames": 3}
        with self.assertRaisesRegex(BatchError, "Invalid measured"):
            expected_writes(plan, timing, snap)

    def test_pending_write_recovery_returns_receipt_without_resending(self):
        from videobatch.sheets import writeback, ownership_path, entered_cells, entered
        # Transport is isolated here; actual live reads are separately verified.
        snap = fixture()
        plan = analyze_sheet(snap, snap["sheets"][0])
        prepared = {"source_hash": plan["source_hash"], "cuts": [{"row": r, "frames": 30} for r in [4, 5, 6]], "total_frames": 90}
        writes = expected_writes(plan, prepared, snap)
        before_cells = entered_cells(snap["sheets"][0])
        before = {k: entered(before_cells, *map(int, k.split(":"))) for k in writes}
        updated = copy.deepcopy(snap)
        for key, val in writes.items():
            row, col = map(int, key.split(":"))
            put(updated, row, col, val)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            prepared_path = root / "prepared.json"
            save_json(prepared_path, prepared)
            journal = {"status": "pending", "source_hash": plan["source_hash"],
                       "prepared_hash": file_hash(prepared_path), "total_row": 7,
                       "before": before, "writes": writes}
            save_json(ownership_path(root / "state", plan) / "journal.json", journal)
            with patch("videobatch.sheets.validate_prepared"), patch("videobatch.sheets.live_snapshot", return_value=updated), \
                    patch("videobatch.sheets.run") as transport:
                result = writeback(plan, prepared_path, root / "state", apply=True, edit_window=True)
                self.assertEqual(result["status"], "confirmed")
                self.assertTrue(result["resumed"])
                transport.assert_not_called()

    def test_immediate_recheck_stops_before_api_mutation(self):
        from videobatch.sheets import writeback
        snap = fixture()
        plan = analyze_sheet(snap, snap["sheets"][0])
        prepared = {"source_hash": plan["source_hash"], "cuts": [{"row": r, "frames": 30} for r in [4, 5, 6]], "total_frames": 90}
        changed = copy.deepcopy(snap)
        put(changed, 4, 7, {"numberValue": 777})
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            save_json(root / "prepared.json", prepared)
            with patch("videobatch.sheets.validate_prepared"), \
                    patch("videobatch.sheets.live_snapshot", side_effect=[snap, changed]), \
                    patch("videobatch.sheets.run") as transport:
                with self.assertRaisesRegex(BatchError, "immediately before"):
                    writeback(plan, root / "prepared.json", root / "state", apply=True, edit_window=True)
                transport.assert_not_called()

    def test_formula_recalculation_is_detected_before_and_after_restore(self):
        from videobatch.sheets import require_plan_unchanged
        snap = fixture()
        put(snap, 4, 1, {"formulaValue": '=OtherTab!B1'})
        snap["sheets"][0]["data"][0]["rowData"][3]["values"][1]["effectiveValue"] = {"stringValue": "元の本文"}
        plan = analyze_sheet(snap, snap["sheets"][0])
        updated = copy.deepcopy(snap)
        updated["sheets"][0]["data"][0]["rowData"][3]["values"][1]["effectiveValue"] = {"stringValue": "再計算された本文"}
        self.assertEqual(sheet_fingerprint(updated["sheets"][0]), plan["source_hash"])
        with self.assertRaisesRegex(BatchError, "Formula results"):
            require_plan_unchanged(plan, updated["sheets"][0])
        put(updated, 4, 7, {"numberValue": 1})
        restore_entered(updated["sheets"][0], {"4:7": {}})
        with self.assertRaisesRegex(BatchError, "Formula results"):
            require_plan_unchanged(plan, updated["sheets"][0])


if __name__ == "__main__":
    unittest.main()
