"""Verify native V2 host credential import with an isolated synthetic store."""

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from oc_box import host_backup_auth as auth
from oc_box import host_backup_restore as restore


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--opencode", type=Path, required=True)
    options = parser.parse_args()
    rows = [
        {
            "id": "cred_syntheticone",
            "integrationID": "provider.synthetic",
            "label": "Synthetic",
            "active": True,
            "value": {"type": "key", "key": "opaque-synthetic"},
        },
        {
            "id": "cred_synthetictwo",
            "integrationID": "mcp.synthetic",
            "label": "Synthetic MCP",
            "active": True,
            "value": {
                "type": "oauth",
                "methodID": "oauth",
                "access": "opaque-synthetic-access",
                "refresh": "opaque-synthetic-refresh",
                "expires": 2_000_000_000,
                "metadata": {"clientId": "synthetic-client"},
            },
        },
    ]
    try:
        with tempfile.TemporaryDirectory(prefix="oc-box-auth-smoke-") as directory:
            data_home = Path(directory) / "data"
            os.environ["XDG_DATA_HOME"] = str(data_home)
            os.environ["XDG_CONFIG_HOME"] = str(Path(directory) / "config")
            plan = restore.preflight({}, include_opencode=True, native_database=True)
            result, database = auth.prepare_native_export(
                json.dumps(rows).encode(),
                binary=options.opencode.absolute(),
                data_home=data_home,
                confirmed=True,
            )
            if result.count != len(rows):
                raise ValueError
            if (
                data_home.exists()
                or restore.apply(plan, include_opencode=True, native_database=database)
                != 1
            ):
                raise ValueError
            db = data_home / "opencode/opencode.db"
            metadata = db.stat()
            if metadata.st_uid != os.getuid() or metadata.st_mode & 0o777 != 0o600:
                raise ValueError
            before = db.read_bytes()
            try:
                auth.check_native_destination(data_home)
            except auth.NativeAuthError:
                pass
            else:
                raise ValueError
            if db.read_bytes() != before:
                raise ValueError
    except auth.NativeAuthError as error:
        parser.exit(1, f"Native V2 host credential smoke refused: {error.reason}\n")
    except (OSError, ValueError):
        parser.exit(1, "Native V2 host credential smoke verification failed.\n")
    print(
        f"PASS: synthetic native {result.version} credentials verified and published once."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
