"""PEP 517 build entrypoint; build implementation lives under src/."""

from pathlib import Path
import runpy

runpy.run_path(
    str(Path(__file__).resolve().parent / "src/_oc_box_build.py"), run_name="__main__"
)
