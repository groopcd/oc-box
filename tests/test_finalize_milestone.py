import importlib.util
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

APP = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("finalize_milestone", APP / "tools/finalize_milestone.py")
finalize = importlib.util.module_from_spec(spec)
spec.loader.exec_module(finalize)
box_spec = importlib.util.spec_from_file_location("oc_box_capture", APP / "oc_box.py")
box = importlib.util.module_from_spec(box_spec)
box_spec.loader.exec_module(box)


class ExportCaptureTests(unittest.TestCase):
    def test_large_stdout_uses_regular_file_and_remains_complete(self):
        # Fail explicitly if a future refactor regresses to subprocess.PIPE.
        program = "import json,os,stat,sys; assert stat.S_ISREG(os.fstat(sys.stdout.fileno()).st_mode); print(json.dumps({'text':'x'*4000000})); print('stderr marker',file=sys.stderr)"
        for runner in (finalize.run, box.run):
            with self.subTest(runner=runner.__module__):
                result = runner([sys.executable, "-c", program], capture=True)
                self.assertEqual(len(json.loads(result.stdout)["text"]), 4000000)
                self.assertIn("stderr marker", result.stderr)

    def test_invalid_export_fails_before_backup_commit_or_import(self):
        result = subprocess.CompletedProcess([], 0, '{"info":{"id":"ses_test"},"messages":["partial', "")
        with patch.object(finalize, "run", return_value=result):
            with self.assertRaisesRegex(RuntimeError, "no session import was attempted"):
                finalize.export_session("ses_test", {})

    def test_backup_is_private_and_contains_full_transcript(self):
        with tempfile.TemporaryDirectory() as root, patch.dict(os.environ, {"XDG_STATE_HOME": root}):
            transcript = {"info": {"id": "ses_test"}, "messages": [{"text": "x" * 4000000}]}
            target = finalize.backup_session(transcript)
            self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o600)
            self.assertEqual(json.loads(target.read_text()), transcript)
            self.assertFalse(target.is_relative_to(APP))

    def test_backup_and_cli_share_same_data_root_and_new_workdir(self):
        with finalize.opencode_env() as env:
            self.assertEqual(env["XDG_DATA_HOME"], os.environ.get("XDG_DATA_HOME", str(Path.home() / ".local/share")))
            self.assertEqual(env["OPENCODE_PURE"], "1")
            result = finalize.run([sys.executable, "-c", "import os; print(os.getcwd()); print(os.environ['PWD'])"], env=env, capture=True)
            self.assertEqual(result.stdout.splitlines(), [str(APP), str(APP)])


if __name__ == "__main__":
    unittest.main()
