import json
from pathlib import Path
import sys
import unittest
from oc_box import oc_box as box

APP = Path(__file__).resolve().parents[1]


class ProcessCaptureTests(unittest.TestCase):
    def test_large_stdout_uses_regular_file_and_remains_complete(self):
        # Mainline can truncate large exports when stdout is a pipe.
        program = "import json,os,stat,sys; assert stat.S_ISREG(os.fstat(sys.stdout.fileno()).st_mode); print(json.dumps({'text':'x'*4000000})); print('stderr marker',file=sys.stderr)"
        result = box.run([sys.executable, "-c", program], capture=True)
        self.assertEqual(len(json.loads(result.stdout)["text"]), 4000000)
        self.assertIn("stderr marker", result.stderr)

    def test_capture_preserves_explicit_cwd_and_pwd(self):
        result = box.run(
            [
                sys.executable,
                "-c",
                "import os; print(os.getcwd()); print(os.environ['PWD'])",
            ],
            cwd=APP,
            capture=True,
        )
        self.assertEqual(result.stdout.splitlines(), [str(APP), str(APP)])


if __name__ == "__main__":
    unittest.main()
