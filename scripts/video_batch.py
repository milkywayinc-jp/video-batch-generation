#!/usr/bin/env python3
"""Run explicitly; --help/import never contacts Sheets or edits media."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from videobatch.common import BatchError, new_directory, read_json, require, save_json
from videobatch.sheets import analyze_sheet, edit_template, live_snapshot, writeback


def parser():
    p = argparse.ArgumentParser(description="★台本 → 音声・尺 → 正方形動画（明示実行のみ）")
    commands = p.add_subparsers(dest="command", required=True)
    inspect = commands.add_parser("inspect", help="Read ★ sheets; create local plans only")
    source = inspect.add_mutually_exclusive_group(required=True)
    source.add_argument("--spreadsheet-id")
    source.add_argument("--snapshot", help="Offline Sheets get response including merges and grid data")
    inspect.add_argument("--out", required=True)
    adjust = commands.add_parser("adjust-audio", help="Adjust an already acquired narration batch locally")
    adjust.add_argument("--plan", required=True)
    adjust.add_argument("--batch-id", required=True)
    adjust.add_argument("--input", required=True)
    adjust.add_argument("--out", required=True, help="New directory")
    prepare = commands.add_parser("prepare", help="Validate editing decisions and measure row audio locally")
    prepare.add_argument("--plan", required=True)
    prepare.add_argument("--edit", required=True)
    prepare.add_argument("--out", required=True)
    write = commands.add_parser("writeback", help="Default: preview; --apply explicitly writes only G/H values")
    write.add_argument("--plan", required=True)
    write.add_argument("--prepared", required=True)
    write.add_argument("--state-dir", required=True, help="Same shared local state directory for all runs")
    write.add_argument("--apply", action="store_true")
    write.add_argument("--edit-window-confirmed", action="store_true")
    render = commands.add_parser("render", help="Render and measure a local MP4")
    render.add_argument("--plan", required=True)
    render.add_argument("--prepared", required=True)
    render.add_argument("--out", required=True)
    mode = render.add_mutually_exclusive_group(required=True)
    mode.add_argument("--local-only", action="store_true", help="No sheet write; output is local validation only")
    mode.add_argument("--receipt", help="Confirmed writeback journal for this prepared file")
    return p


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        if args.command == "inspect":
            snapshot = read_json(args.snapshot) if args.snapshot else live_snapshot(args.spreadsheet_id)
            out = new_directory(args.out)
            save_json(out / "snapshot.json", snapshot)
            results = []
            for sheet in snapshot.get("sheets", []):
                if "★" not in sheet["properties"]["title"]:
                    continue
                sid = sheet["properties"]["sheetId"]
                try:
                    plan = analyze_sheet(snapshot, sheet)
                    save_json(out / f"{sid}.plan.json", plan)
                    save_json(out / f"{sid}.edit.json", edit_template(plan))
                    results.append({"sheet_id": sid, "cuts": len(plan["cuts"]),
                                    "batches": len(plan["batches"]), "status": "planned",
                                    "pending_material_rows": plan["known_pending"]["material_rows"]})
                except BatchError as exc:
                    results.append({"sheet_id": sid, "status": "blocked", "error": str(exc)})
            require(results, "No ★ sheets in this snapshot")
            save_json(out / "inspection.json", results)
            print(json.dumps(results, ensure_ascii=False))
            return 2 if any(r["status"] == "blocked" for r in results) else 0
        if args.command == "writeback":
            result = writeback(read_json(args.plan), args.prepared, args.state_dir,
                               args.apply, args.edit_window_confirmed)
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0
        from videobatch.media import adjust_audio, prepare_media, render_media
        plan = read_json(args.plan)
        if args.command == "adjust-audio":
            result = adjust_audio(plan, args.batch_id, args.input, args.out)
        elif args.command == "prepare":
            result = prepare_media(plan, read_json(args.edit), args.out)
        else:
            result = render_media(plan, args.prepared, args.out, args.receipt, args.local_only)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (BatchError, OSError, ValueError, KeyError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
