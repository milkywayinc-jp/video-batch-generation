import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


class DistributionTests(unittest.TestCase):
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
