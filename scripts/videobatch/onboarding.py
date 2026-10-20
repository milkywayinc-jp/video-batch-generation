"""Explicit setup only: pinned native copy in the signed-in user's My Drive.

No imports/help/download hooks, credential storage, permissions changes or
production-chat side effects. The setup skill uses official chat tools separately.
"""
from __future__ import annotations

import contextlib
import fcntl
import json
import re
import uuid
from pathlib import Path

from .common import BatchError, digest, read_json, require, run, save_json
from .template import verify_template


def drive(resource, method, params=None, body=None):
    args = ["gws", "drive", resource, method, "--params", json.dumps(params or {})]
    if body is not None:
        args += ["--json", json.dumps(body)]
    return json.loads(run(args).stdout)


@contextlib.contextmanager
def setup_lock(state_dir):
    root = Path(state_dir).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    ignore = root / ".gitignore"
    if not ignore.exists():
        ignore.write_text("*\n", encoding="utf-8")
    with (root / "setup.lock").open("a") as f:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise BatchError("Setup is already running for this workspace") from exc
        try:
            yield root
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def own_file(file_id):
    result = drive("files", "get", {"fileId": file_id,
                   "fields": "id,name,ownedByMe,trashed,parents,driveId,mimeType,capabilities(canCopy,canAddChildren)"})
    require(result.get("ownedByMe") is True and not result.get("trashed") and not result.get("driveId"),
            "Expected the signed-in user's non-trashed My Drive file")
    return result


def setup(source_id, release, state_dir, apply=False, reuse_id=None, create_another=False):
    require(not (reuse_id and create_another), "Choose reuse OR another copy")
    require(re.fullmatch(r"[A-Za-z0-9_-]+", source_id or ""), "Use a raw native spreadsheet ID")
    with setup_lock(state_dir) as root:
        journal_path = root / "setup.json"
        journal = read_json(journal_path) if journal_path.exists() else None
        source_fingerprint = digest(source_id)
        if journal:
            require(journal["source_fingerprint"] == source_fingerprint
                    and journal["release_version"] == release["version"], "Another template is bound to this workspace")
            if journal.get("copy_id"):
                own_file(journal["copy_id"])
                if journal["status"] == "copy_unverified":
                    snapshot = verify_template(journal["copy_id"], release)
                    return confirm_copy(root, journal, journal["copy_id"], snapshot, release)
                return {**journal, "resumed": True}
            if journal.get("status") == "copy_pending":
                found = drive("files", "list", {"q": "trashed = false and 'me' in owners and "
                              f"properties has {{ key='vb_transaction' and value='{journal['transaction_id']}' }}",
                              "fields": "files(id,name,parents),nextPageToken", "pageSize": 100})
                require(not found.get("nextPageToken") and len(found.get("files", [])) == 1,
                        "Copy outcome is unknown or ambiguous. Reconcile by transaction ID; do not resend")
                copy_id = found["files"][0]["id"]
                journal.update(copy_id=copy_id, status="copy_unverified")
                save_json(journal_path, journal)
                # Recovery depends on the owned copy, not continued source access.
                snapshot = verify_template(copy_id, release)
                return confirm_copy(root, journal, copy_id, snapshot, release)
        snapshot = verify_template(source_id, release)
        source = drive("files", "get", {"fileId": source_id,
                       "fields": "id,name,mimeType,trashed,capabilities(canCopy)"})
        require(source.get("mimeType") == "application/vnd.google-apps.spreadsheet"
                and not source.get("trashed") and source.get("capabilities", {}).get("canCopy") is True,
                "Source must be a readable, copyable native spreadsheet")
        if not apply:
            return {"status": "dry_run", "release_version": release["version"],
                    "verified_template": True, "destination": "My Drive / ChatGPT",
                    "effects_on_apply": ["create/reuse own ChatGPT folder", "copy pinned workbook including script text"],
                    "chat_creation": "setup skill only; requires available official chat tools"}
        # Query before creating, including after clone/local-state loss. Do not
        # guess which of several prior copies a user wants to reuse.
        query = ("trashed = false and 'me' in owners and "
                 f"properties has {{ key='vb_source' and value='{source_fingerprint}' }} and "
                 f"properties has {{ key='vb_release' and value='{release['version']}' }}")
        candidates = drive("files", "list", {"q": query, "fields": "files(id,name,parents),nextPageToken", "pageSize": 100})
        matches = candidates.get("files", [])
        require(not candidates.get("nextPageToken"), "Too many existing copies; select one explicitly")
        if journal and journal.get("transaction_id"):
            matches = drive("files", "list", {"q": "trashed = false and 'me' in owners and "
                            f"properties has {{ key='vb_transaction' and value='{journal['transaction_id']}' }}",
                            "fields": "files(id,name,parents),nextPageToken", "pageSize": 100})
            require(not matches.get("nextPageToken"), "Ambiguous pending copy")
            matches = matches.get("files", [])
        if matches and not reuse_id and not create_another:
            return {"status": "existing_copy_selection_required", "candidates": matches,
                    "next": "Confirm --reuse-copy-id ID or explicitly --create-another; no copy sent"}
        require(not reuse_id or reuse_id in {m["id"] for m in matches}, "Chosen copy is not a matching owned candidate")
        if reuse_id:
            own_file(reuse_id)
            journal = journal or {"source_fingerprint": source_fingerprint, "source_id": source_id,
                                  "release_version": release["version"], "status": "source_selected"}
            return confirm_copy(root, journal, reuse_id, snapshot, release, recovered=True)
        require(not journal or journal.get("status") != "copy_pending",
                "Copy outcome is unknown. Reconcile by transaction ID; do not resend because search returned zero")
        folders = drive("files", "list", {"q": "trashed = false and 'me' in owners and 'root' in parents and "
                        "mimeType='application/vnd.google-apps.folder' and name='ChatGPT'",
                        "fields": "files(id,name),nextPageToken", "pageSize": 100})
        require(not folders.get("nextPageToken") and len(folders.get("files", [])) <= 1,
                "Multiple ChatGPT folders; select a destination first")
        folder = folders["files"][0] if folders.get("files") else drive("files", "create", {"fields": "id,name"},
                        {"name": "ChatGPT", "mimeType": "application/vnd.google-apps.folder", "parents": ["root"]})
        require(own_file(folder["id"]).get("capabilities", {}).get("canAddChildren") is True,
                "Destination folder is not writable")
        permissions = drive("permissions", "list", {"fileId": folder["id"], "fields": "permissions(role),nextPageToken"})
        require(not permissions.get("nextPageToken") and
                all(p["role"] == "owner" for p in permissions.get("permissions", [])),
                "ChatGPT folder is shared; choose a private My Drive destination before copying private scripts")
        transaction = str(uuid.uuid4())
        journal = {"schema_version": 1, "status": "copy_pending", "transaction_id": transaction,
                   "source_id": source_id, "source_fingerprint": source_fingerprint,
                   "release_version": release["version"], "folder_id": folder["id"]}
        save_json(journal_path, journal)  # durable before the external copy
        copied = drive("files", "copy", {"fileId": source_id, "fields": "id,name,parents", "ignoreDefaultVisibility": True},
                       {"name": f"動画_素材生成用_{release['version']}_{transaction[:8]}", "parents": [folder["id"]],
                        "properties": {"vb_transaction": transaction, "vb_source": source_fingerprint,
                                       "vb_release": release["version"]}})
        require(copied["id"] != source_id, "Copy must have a different ID")
        # Store identity before checking its contents so a validation/network
        # failure cannot cause a duplicate copy on retry.
        journal.update(copy_id=copied["id"], status="copy_unverified")
        save_json(journal_path, journal)
        return confirm_copy(root, journal, copied["id"], snapshot, release)


def confirm_copy(root, journal, copy_id, snapshot, release, recovered=False):
    own_file(copy_id)
    # Recovered copies may already be edited by their user. A new/unverified
    # copy must still match the pinned release before setup can continue.
    if not recovered or journal.get("status") in ("copy_pending", "copy_unverified"):
        verify_template(copy_id, release)
    journal.update(status="copy_confirmed", copy_id=copy_id,
                   copy_url=f"https://docs.google.com/spreadsheets/d/{copy_id}/edit")
    save_json(root / "template.snapshot.json", snapshot)
    save_json(root / "setup.json", journal)
    prompt = ("動画一括生成の制作チャットです。制作ルールは毎回このスプレッドシートから読み取ってください。\\n"
              f"制作シート：{journal['copy_url']}\\n配布基準版：{journal['release_version']}\\n"
              "全体→共通→個別指定を解釈し、必要素材と接続を確認してから制作してください。"
              "シートの値をスキルの固定ルールへ転記しないでください。"
              "元の保存先・素材フォルダ・引継ぎ先は、この利用者が使える場所か確認してください。"
              "セットアップだけで動画生成・課金処理・外部投稿を開始しないでください。\\n")
    (root / "production-chat-prompt.txt").write_text(prompt.replace("\\n", "\n"), encoding="utf-8")
    return {**journal, "next": "review destinations, then create/confirm the production chat using setup skill",
            "start_prompt": str(root / "production-chat-prompt.txt")}


def finish_setup(state_dir, destinations, chat_id=None, handoff=False):
    with setup_lock(state_dir) as root:
        journal = read_json(root / "setup.json")
        require(journal["status"] in ("copy_confirmed", "destinations_confirmed", "production_chat_ready", "chat_handoff_ready"),
                "Verify the pinned copy before finishing setup")
        require(set(destinations) == {"material", "output", "handoff"}, "Review material/output/handoff destinations")
        require(all(isinstance(v, str) and v.strip() for v in destinations.values()),
                "Record each confirmed destination or explicitly 'none'/'pending'")
        journal.update(destinations=destinations, status="destinations_confirmed")
        require((bool(chat_id) and not handoff) or (handoff and not chat_id), "Provide verified chat ID OR manual handoff")
        if chat_id:
            journal.update(chat_id=chat_id, status="production_chat_ready")
        else:
            journal.update(status="chat_handoff_ready")
        journal["production_inputs_ready"] = not any(v.lower() == "pending" for v in destinations.values())
        save_json(root / "setup.json", journal)
        return journal
