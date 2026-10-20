from __future__ import annotations

import contextlib
import fcntl
import json
import os
import re
import tempfile
import copy
from pathlib import Path

from .common import BatchError, digest, file_hash, read_json, require, run, save_json, validate_prepared


def gws_get(spreadsheet_id, **kwargs):
    params = {"spreadsheetId": spreadsheet_id, **kwargs}
    response = run(["gws", "sheets", "spreadsheets", "get", "--params", json.dumps(params)])
    return json.loads(response.stdout)


def live_snapshot(spreadsheet_id, sheet_id=None):
    first = _live_snapshot_once(spreadsheet_id, sheet_id)
    second = _live_snapshot_once(spreadsheet_id, sheet_id)
    require(digest(first) == digest(second), "Sheet changed during paged read; inspect again")
    return second


def _live_snapshot_once(spreadsheet_id, sheet_id=None):
    require(re.fullmatch(r"[A-Za-z0-9_-]+", spreadsheet_id or ""), "Invalid spreadsheet ID")
    meta = gws_get(spreadsheet_id, fields="spreadsheetId,sheets(properties,merges,conditionalFormats)")
    targets = [s for s in meta.get("sheets", []) if "★" in s["properties"]["title"]
               and (sheet_id is None or s["properties"]["sheetId"] == sheet_id)]
    require(targets, "No matching ★ sheet; no cells changed")
    result = {"spreadsheetId": spreadsheet_id, "sheets": [], "source": "live-gws"}
    for target in targets:
        props = target["properties"]
        grid = props.get("gridProperties", {})
        require(grid.get("columnCount", 0) >= 11, f"{props['title']}: expected A:K")
        title = "'" + props["title"].replace("'", "''") + "'"
        sheet = {**target, "data": []}
        for first in range(1, grid["rowCount"] + 1, 400):
            last = min(first + 399, grid["rowCount"])
            chunk = gws_get(spreadsheet_id, ranges=[f"{title}!A{first}:K{last}"],
                            includeGridData=True,
                            fields="sheets(properties,merges,data(startRow,startColumn,rowData(values(userEnteredValue,effectiveValue,formattedValue))))")
            matches = [s for s in chunk.get("sheets", []) if s["properties"]["sheetId"] == props["sheetId"]]
            require(len(matches) == 1, "Sheet changed while reading; inspect again")
            current = matches[0]
            require(current["properties"] == props and current.get("merges", []) == target.get("merges", []),
                    "Sheet dimensions/title/merges changed while reading; inspect again")
            sheet["data"].extend(current.get("data", []))
        result["sheets"].append(sheet)
    return result


def entered_cells(sheet):
    cells = {}
    for block in sheet.get("data", []):
        for r, row in enumerate(block.get("rowData", []), block.get("startRow", 0) + 1):
            for c, cell in enumerate(row.get("values", []), block.get("startColumn", 0)):
                if cell.get("userEnteredValue"):
                    cells[(r, c)] = cell
    return cells


def entered(cells, row, col):
    return cells.get((row, col), {}).get("userEnteredValue", {})


def value(cells, row, col):
    cell = cells.get((row, col), {})
    val = cell.get("effectiveValue") or cell.get("userEnteredValue", {})
    if "errorValue" in val:
        raise BatchError(f"Cell {row}:{col + 1} has a formula error")
    for key in ("stringValue", "numberValue", "boolValue"):
        if key in val:
            return val[key]
    return cell.get("formattedValue", "") if "formulaValue" in val else ""


def inherited(sheet, cells, row, col):
    for merge in sheet.get("merges", []):
        if merge["startRowIndex"] <= row - 1 < merge["endRowIndex"] and \
                merge["startColumnIndex"] <= col < merge["endColumnIndex"]:
            return value(cells, merge["startRowIndex"] + 1, merge["startColumnIndex"])
    return value(cells, row, col)


def sheet_fingerprint(sheet):
    return digest({"properties": sheet["properties"], "merges": sheet.get("merges", []),
                   "cells": {f"{r}:{c}": cell["userEnteredValue"]
                             for (r, c), cell in entered_cells(sheet).items()}})


def make_batches(cuts, rules):
    from .rules import cut_rules
    batches = []
    current = None
    for cut in cuts:
        resolved = cut_rules(rules, cut["row"])
        if resolved["mode"] != "narration":
            current = None
            continue
        text = cut["speech"]
        limit = resolved["audio"]["batch_limit"]
        group = resolved["audio"]
        if current and current["audio_rules"] != group:
            current = None
        require(len(text) <= limit and len(text.encode("utf-16-le")) // 2 <= limit,
                f"Row {cut['row']}: a speech exceeds sheet limit {limit}; split into verified sub-batches first")
        candidate = (current["text"] + "\n" if current else "") + text
        if current is None or len(candidate) > limit or len(candidate.encode("utf-16-le")) // 2 > limit:
            current = {"id": f"batch-{len(batches) + 1:03}", "text": "", "rows": [], "offsets": [],
                       "audio_rules": group}
            batches.append(current)
        start = len(current["text"]) + (1 if current["rows"] else 0)
        current["text"] += ("\n" if current["rows"] else "") + text
        current["rows"].append(cut["row"])
        current["offsets"].append({"row": cut["row"], "start": start, "end": start + len(text)})
    for batch in batches:
        batch["text_hash"] = digest(batch["text"])
    return batches


def analyze_sheet(snapshot, sheet):
    props = sheet["properties"]
    require("★" in props["title"], "Non-★ sheet is excluded")
    cells = entered_cells(sheet)
    headers = [r for r, c in cells if c == 7 and value(cells, r, 7) == "秒数"
               and value(cells, r, 8) == "音声生成用テキスト"
               and value(cells, r, 5) == "素材名"]
    require(len(headers) == 1, f"{props['title']}: cannot uniquely identify headers")
    header = headers[0]
    require(value(cells, header, 3) == "注釈①" and value(cells, header, 4) == "注釈②"
            and value(cells, header, 9) == "音声個別指定", "Unsupported header layout")
    caption_header = inherited(sheet, cells, header, 1)
    require(caption_header == "テロップ", "Cannot resolve caption column B from merged header")
    cuts, orphans, old_totals = [], [], []
    for row in range(header + 1, props["gridProperties"]["rowCount"] + 1):
        primary = [value(cells, row, c) for c in (1, 5, 8)]
        if value(cells, row, 6) == "合計秒数" and not any(primary):
            old_totals.append(row)
            continue
        if not any(primary):
            local_global = [c for c in (4, 10) if value(cells, row, c) and not any(
                m["startRowIndex"] <= row - 1 < m["endRowIndex"]
                and m["startColumnIndex"] <= c < m["endColumnIndex"]
                and m["endRowIndex"] - m["startRowIndex"] > 1 for m in sheet.get("merges", []))]
            if any(value(cells, row, c) for c in (2, 3, 6, 9)) or local_global:
                orphans.append(row)
            continue
        custom = str(value(cells, row, 9))
        mode = "source" if custom == "動画の音声をそのまま使用" else ("narration" if primary[2] else "silence")
        cuts.append({"row": row, "caption": str(primary[0]), "speech": str(primary[2]),
                     "material": str(primary[1]), "audio_mode": mode,
                     "note1": str(inherited(sheet, cells, row, 3)),
                     "note2": str(inherited(sheet, cells, row, 4)),
                     "bgm": str(inherited(sheet, cells, row, 10)),
                     "directives": {"caption": str(value(cells, row, 2)),
                                    "cut": str(value(cells, row, 6)), "audio": custom}})
    require(cuts, "No script cuts")
    require(not orphans, f"Orphan individual directives at rows {orphans}; resolve before editing")
    settings = {"global": str(value(cells, header - 2, 0)), "memo": str(value(cells, header - 2, 5)),
                "caption": str(inherited(sheet, cells, header - 1, 0)),
                "note1": str(value(cells, header - 1, 3)),
                "note2": str(value(cells, header - 1, 4)),
                "material": str(inherited(sheet, cells, header - 1, 5)),
                "audio": str(inherited(sheet, cells, header - 1, 7)),
                "bgm": str(value(cells, header - 1, 10))}
    # Store the rule cells themselves, including formula results, rather than
    # turning their current editing preferences into Python defaults.
    coordinates = [(header - 2, 0), (header - 2, 5)] + [(header - 1, c) for c in (0, 3, 4, 5, 7, 10)]
    coordinates += [(cut["row"], c) for cut in cuts for c in (2, 6, 9)]
    inputs = {}
    for row, col in coordinates:
        if value(cells, row, col) != "":
            key = f"{chr(65 + col)}{row}"
            inputs[key] = {"entered": entered(cells, row, col), "effective": value(cells, row, col)}
    return {"schema_version": 2, "spreadsheet_id": snapshot["spreadsheetId"],
            "source_kind": snapshot.get("source", "offline"),
            "sheet_id": props["sheetId"], "sheet_name": props["title"],
            "source_hash": sheet_fingerprint(sheet), "header_row": header,
            "last_cut_row": cuts[-1]["row"], "total_row": cuts[-1]["row"] + 1,
            "old_total_candidates": old_totals, "settings": settings, "cuts": cuts,
            "rule_inputs": inputs,
            "known_pending": {"material_rows": [c["row"] for c in cuts if not c["material"]],
                              "material_folder": "Resolve from settings or explicit local path",
                              "output_folder": "Explicit local output path required"}}


def edit_template(plan):
    from .rules import validate_rules
    rules = validate_rules(plan)
    return {"schema_version": 2, "source_hash": plan["source_hash"],
            "resolved_rules_hash": rules["integrity"], "fonts": {}, "material_root": "",
            "batches": [{"id": b["id"], "path": "", "boundaries": [], "verified": False}
                        for b in plan["batches"]],
            "cuts": [{"row": c["row"], "video_path": "", "video_start": None,
                      "source_intervals": [], "source_verified": False,
                      "silent_frames": None, "bgm_path": ""} for c in plan["cuts"]]}


def find_sheet(snapshot, sheet_id):
    matches = [s for s in snapshot["sheets"] if s["properties"]["sheetId"] == sheet_id]
    require(len(matches) == 1, "Source sheet is missing or duplicated")
    return matches[0]


def require_plan_unchanged(plan, sheet):
    require(sheet_fingerprint(sheet) == plan["source_hash"], "Sheet changed; inspect and prepare again")
    # Formula text can stay the same while referenced values change.
    from .rules import raw_plan
    current_plan = analyze_sheet({"spreadsheetId": plan["spreadsheet_id"], "source": plan.get("source_kind", "offline")}, sheet)
    require(digest(current_plan) == digest(raw_plan(plan)),
            "Formula results or script interpretation changed; inspect and prepare again")


def ownership_path(state_root, plan):
    return Path(state_root) / digest([plan["spreadsheet_id"], plan["sheet_id"]])


@contextlib.contextmanager
def sheet_lock(state_root, plan):
    root = ownership_path(state_root, plan)
    root.mkdir(parents=True, exist_ok=True)
    lock_root = Path(tempfile.gettempdir()) / f"video-batch-write-locks-{os.getuid()}"
    lock_root.mkdir(mode=0o700, exist_ok=True)
    with open(lock_root / (root.name + ".lock"), "a") as f:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise BatchError("Another local run is writing this sheet") from exc
        try:
            yield root
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def expected_writes(plan, prepared, snapshot, previous=None):
    require(prepared["source_hash"] == plan["source_hash"], "Timing is from another plan")
    require([c["row"] for c in prepared["cuts"]] == [c["row"] for c in plan["cuts"]], "Timing rows mismatch")
    sheet = find_sheet(snapshot, plan["sheet_id"])
    require_plan_unchanged(plan, sheet)
    cells = entered_cells(sheet)
    end = plan["total_row"]
    require(end <= sheet["properties"]["gridProperties"]["rowCount"], "A blank row is needed after the last cut")
    updates = {}
    owned = previous.get("writes", {}) if previous and previous.get("status") == "confirmed" else {}
    for old in plan.get("old_total_candidates", []):
        require(owned.get(f"{old}:6") == entered(cells, old, 6)
                and owned.get(f"{old}:7") == entered(cells, old, 7),
                "An unowned old total exists; reconcile it before writing another total")
    # Clear only the previous total, never a user entry, never all historic H cells.
    old_row = previous.get("total_row") if owned else None
    if old_row and old_row != end:
        for col in (6, 7):
            key = f"{old_row}:{col}"
            old = owned.get(key)
            if old is not None:
                require(entered(cells, old_row, col) == old, "Previous total was edited; no cells changed")
                updates[key] = {}
    for col in (6, 7):
        key = f"{end}:{col}"
        existing = entered(cells, end, col)
        require(not existing or owned.get(key) == existing, "Total destination contains unowned data")
    for cut in prepared["cuts"]:
        require(isinstance(cut["frames"], int) and not isinstance(cut["frames"], bool)
                and cut["frames"] > 0, "Invalid measured frame count")
        from .rules import Timebase
        tb = Timebase(prepared["fps"])
        updates[f"{cut['row']}:7"] = {"numberValue": tb.seconds(cut["frames"])}
    require(isinstance(prepared["total_frames"], int) and not isinstance(prepared["total_frames"], bool)
            and sum(c["frames"] for c in prepared["cuts"]) == prepared["total_frames"], "Timing total mismatch")
    updates[f"{end}:6"] = {"stringValue": "合計秒数"}
    updates[f"{end}:7"] = {"numberValue": tb.seconds(prepared["total_frames"])}
    return updates


def update_request(plan, writes):
    requests = []
    for key, val in sorted(writes.items()):
        row, col = map(int, key.split(":"))
        requests.append({"updateCells": {"range": {"sheetId": plan["sheet_id"],
                         "startRowIndex": row - 1, "endRowIndex": row,
                         "startColumnIndex": col, "endColumnIndex": col + 1},
                         "rows": [{"values": [{"userEnteredValue": val} if val else {}]}],
                         "fields": "userEnteredValue"}})
    return {"requests": requests}


def writeback(plan, prepared_path, state_root, apply=False, edit_window=False):
    prepared = read_json(prepared_path)
    validate_prepared(plan, prepared)
    with sheet_lock(state_root, plan) as root:
        journal_path = root / "journal.json"
        previous = read_json(journal_path) if journal_path.exists() else None
        snapshot = live_snapshot(plan["spreadsheet_id"], plan["sheet_id"])
        sheet = find_sheet(snapshot, plan["sheet_id"])
        cells = entered_cells(sheet)
        # A verified previous write can be resumed without writing a second time.
        if previous and previous["source_hash"] == plan["source_hash"] \
                and previous["prepared_hash"] == file_hash(prepared_path):
            actual = {k: entered(cells, *map(int, k.split(":"))) for k in previous["writes"]}
            if actual == previous["writes"]:
                restored = copy.deepcopy(sheet)
                restore_entered(restored, previous["before"])
                require_plan_unchanged(plan, restored)
                previous["status"] = "confirmed"
                save_json(journal_path, previous)
                return {"status": "confirmed", "receipt": str(journal_path), "resumed": True}
        if previous and previous["status"] == "pending":
            actual = {k: entered(cells, *map(int, k.split(":"))) for k in previous["writes"]}
            if actual == previous["writes"]:
                previous["status"] = "confirmed"
                save_json(journal_path, previous)
            elif actual == previous["before"]:
                raise BatchError("Previous write did not apply; archive its journal after reviewing it, then retry")
            else:
                raise BatchError("Uncertain write conflicts with current cells; manual reconciliation required")
        writes = expected_writes(plan, prepared, snapshot, previous)
        body = update_request(plan, writes)
        if not apply:
            return {"status": "dry_run", "body": body}
        require(edit_window, "--edit-window-confirmed is required for a live write; read/write is not atomic with human edits")
        before = {k: entered(cells, *map(int, k.split(":"))) for k in writes}
        journal = {"status": "pending", "spreadsheet_id": plan["spreadsheet_id"],
                   "sheet_id": plan["sheet_id"], "source_hash": plan["source_hash"],
                   "prepared_hash": file_hash(prepared_path), "total_row": plan["total_row"],
                   "before": before, "writes": writes}
        save_json(journal_path, journal)
        # Re-read after journaling, immediately before the external update.
        immediate = find_sheet(live_snapshot(plan["spreadsheet_id"], plan["sheet_id"]), plan["sheet_id"])
        require(sheet_fingerprint(immediate) == plan["source_hash"],
                "Sheet changed immediately before write; nothing sent, pending journal retained")
        require_plan_unchanged(plan, immediate)
        run(["gws", "sheets", "spreadsheets", "batchUpdate", "--params",
             json.dumps({"spreadsheetId": plan["spreadsheet_id"]}), "--json", json.dumps(body)])
        after = live_snapshot(plan["spreadsheet_id"], plan["sheet_id"])
        after_sheet = find_sheet(after, plan["sheet_id"])
        after_cells = entered_cells(after_sheet)
        require(all(entered(after_cells, *map(int, k.split(":"))) == v for k, v in writes.items()),
                "Readback mismatch; journal retained pending, do not blindly retry")
        # Restore only our cells in the readback copy to check all other script inputs.
        restore_entered(after_sheet, before)
        require_plan_unchanged(plan, after_sheet)
        journal["status"] = "confirmed"
        save_json(journal_path, journal)
        return {"status": "confirmed", "receipt": str(journal_path)}


def restore_entered(sheet, values):
    cells = dict(entered_cells(sheet))
    for key, val in values.items():
        point = tuple(map(int, key.split(":")))
        if val:
            cells[point] = {"userEnteredValue": val}
        else:
            cells.pop(point, None)
    rows = []
    for row in range(1, max((r for r, _ in cells), default=0) + 1):
        columns = max((c for r, c in cells if r == row), default=-1)
        rows.append({"values": [cells.get((row, col), {}) for col in range(columns + 1)]})
    sheet["data"] = [{"rowData": rows}]
