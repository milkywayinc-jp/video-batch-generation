import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from videobatch.common import BatchError, digest, read_json, save_json
from videobatch.onboarding import setup, finish_setup


RELEASE = {"schema_version": 1, "version": "test-v1", "structure_sha256": "test-only"}
SOURCE = {"id": "source", "mimeType": "application/vnd.google-apps.spreadsheet", "capabilities": {"canCopy": True}}
OWN = {"id": "copied", "ownedByMe": True, "parents": ["folder"]}


class OnboardingTests(unittest.TestCase):
    def test_state_loss_does_not_silently_bind_another_workspaces_copy(self):
        with tempfile.TemporaryDirectory() as directory:
            def transport(resource, method, params=None, body=None):
                return SOURCE if method == "get" else {"files": [{"id": "copied", "name": "existing project"}]}
            with patch("videobatch.onboarding.verify_template", return_value={"sheets": []}), \
                    patch("videobatch.onboarding.drive", side_effect=transport) as calls:
                result = setup("source", RELEASE, directory, True)
                self.assertEqual(result["status"], "existing_copy_selection_required")
                self.assertTrue(all(c.args[1] in ("get", "list") for c in calls.call_args_list))
            self.assertFalse((Path(directory) / "setup.json").exists())

    def pending(self):
        return {"schema_version": 1, "status": "copy_pending", "source_id": "source",
                "source_fingerprint": digest("source"), "release_version": "test-v1", "transaction_id": "test-transaction"}

    def test_unknown_copy_outcome_does_not_resend_on_empty_search(self):
        with tempfile.TemporaryDirectory() as directory:
            save_json(Path(directory) / "setup.json", self.pending())
            calls = []
            def transport(resource, method, params=None, body=None):
                calls.append((resource, method))
                return SOURCE if method == "get" else {"files": []}
            with patch("videobatch.onboarding.verify_template", return_value={"sheets": []}), \
                    patch("videobatch.onboarding.drive", side_effect=transport):
                with self.assertRaisesRegex(BatchError, "outcome is unknown"):
                    setup("source", RELEASE, directory, True)
            self.assertNotIn(("files", "copy"), calls)
            self.assertNotIn(("files", "create"), calls)

    def test_pending_copy_recovery_never_creates_another_file(self):
        with tempfile.TemporaryDirectory() as directory:
            save_json(Path(directory) / "setup.json", self.pending())
            calls = []
            def transport(resource, method, params=None, body=None):
                calls.append((resource, method))
                if method == "get":
                    return SOURCE if params["fileId"] == "source" else OWN
                return {"files": [{"id": "copied", "name": "own copy"}]}
            with patch("videobatch.onboarding.verify_template", side_effect=lambda fid, release: {"sheets": []} if fid == "copied" else self.fail("Original must not be accessed on recovery")), \
                    patch("videobatch.onboarding.drive", side_effect=transport):
                result = setup("source", RELEASE, directory, True)
                self.assertEqual(result["status"], "copy_confirmed")
            self.assertNotIn(("files", "copy"), calls)
            self.assertTrue((Path(directory) / "production-chat-prompt.txt").exists())

    def test_success_response_before_validation_failure_is_recoverable(self):
        with tempfile.TemporaryDirectory() as directory:
            journal = self.pending()
            journal.update(copy_id="copied", status="copy_unverified")
            save_json(Path(directory) / "setup.json", journal)
            with patch("videobatch.onboarding.drive", return_value=OWN) as transport, \
                    patch("videobatch.onboarding.verify_template", side_effect=BatchError("changed")):
                with self.assertRaisesRegex(BatchError, "changed"):
                    setup("source", RELEASE, directory, True)
                self.assertTrue(all(c.args[:2] == ("files", "get") for c in transport.call_args_list))
            self.assertEqual(read_json(Path(directory) / "setup.json")["status"], "copy_unverified")

    def test_existing_edited_copy_is_reused_and_manual_chat_is_not_claimed_created(self):
        with tempfile.TemporaryDirectory() as directory:
            journal = self.pending()
            journal.update(copy_id="copied", status="copy_confirmed")
            save_json(Path(directory) / "setup.json", journal)
            with patch("videobatch.onboarding.drive", return_value=OWN), \
                    patch("videobatch.onboarding.verify_template") as verify:
                result = setup("source", RELEASE, directory, True)
                self.assertTrue(result["resumed"])
                verify.assert_not_called()
            result = finish_setup(directory, {"material": "pending", "output": "pending", "handoff": "none"}, handoff=True)
            self.assertEqual(result["status"], "chat_handoff_ready")
            self.assertFalse(result["production_inputs_ready"])
            self.assertNotIn("chat_id", result)


if __name__ == "__main__":
    unittest.main()
