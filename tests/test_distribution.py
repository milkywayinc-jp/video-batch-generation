import os
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


class DistributionTests(unittest.TestCase):
    def test_plugin_paths_and_onboarding_are_packaged_without_hooks(self):
        root = Path(__file__).resolve().parents[1]
        manifest = json.loads((root / "plugin.json").read_text())
        onboarding = manifest["extensions"]["com.openai"]["onboardingSkill"]
        self.assertTrue((root / onboarding).is_file())
        self.assertNotIn("hooks", manifest["extensions"]["com.openai"])
        self.assertTrue((root / "skills/video-batch-generation/SKILL.md").is_file())
        release = json.loads((root / "templates/releases/2026-10-20-v2.json").read_text())
        self.assertNotIn("source_id", release)
        self.assertEqual(len(release["structure_sha256"]), 64)
        self.assertEqual(release["fingerprint_schema"], 2)

    def test_snapshot_hash_includes_noncell_workbook_structures(self):
        import copy
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
        from videobatch.template import structure_hash
        base = {"properties": {"title": "first", "locale": "ja_JP"}, "sheets": [], "namedRanges": []}
        same = copy.deepcopy(base)
        same["properties"]["title"] = "own copy"
        self.assertEqual(structure_hash(base), structure_hash(same))
        same["namedRanges"].append({"name": "rule", "range": {"sheetId": 123}})
        self.assertNotEqual(structure_hash(base), structure_hash(same))

    def test_help_needs_no_external_command_or_working_directory_write(self):
        script = Path(__file__).resolve().parents[1] / "scripts/video_batch.py"
        with tempfile.TemporaryDirectory() as directory:
            env = {**os.environ, "PATH": "", "PYTHONDONTWRITEBYTECODE": "1"}
            result = subprocess.run([sys.executable, str(script), "--help"], cwd=directory,
                                    env=env, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("inspect", result.stdout)
            self.assertEqual(list(Path(directory).iterdir()), [])


if __name__ == "__main__":
    unittest.main()
