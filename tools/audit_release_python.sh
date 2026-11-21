#!/usr/bin/env bash
# Use a separate scanner environment so pip-audit does not audit its own extras.
set -euo pipefail

target=$1
commit=$2
shift 2
work=$(mktemp -d "${RUNNER_TEMP:-${TMPDIR:-/tmp}}/oc-box-python-audit.XXXXXXXX")
trap 'rm -r -- "$work"' EXIT

python3 -m venv "$work/scanner"
"$work/scanner/bin/python" -m pip install --only-binary=:all: 'pip-audit==2.10.1'

site=$("$target/bin/python" -c 'import sysconfig; print(sysconfig.get_path("purelib"))')
status=0
"$work/scanner/bin/python" -m pip_audit --path "$site" --strict \
    --progress-spinner off --timeout 20 --format json --output "$work/audit.json" \
    || status=$?
python3 tools/check_security_scan.py python "$work/audit.json" \
    --scanner-exit "$status" --commit "$commit" "$@"
