from pathlib import Path
import shutil
import subprocess
import unittest


class StudioTranscriptViewTests(unittest.TestCase):
    @unittest.skipUnless(
        shutil.which("node"), "Node.js is required for the DOM fixture"
    )
    def test_synthetic_dom_contract(self):
        test = Path(__file__).with_name("web_studio_transcript_dom.mjs")
        result = subprocess.run(
            ["node", "--test", str(test)],
            text=True,
            capture_output=True,
            timeout=15,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
