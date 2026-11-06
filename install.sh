#!/bin/sh
# GPL-3.0-or-later. Public bootstrap: no sudo, tokens, global Git config, or hooks.
set -eu

main() {
    if [ "$(id -u)" = 0 ]; then
        printf '%s\n' 'Run the oc-box installer as your regular host user, without sudo.' >&2
        exit 1
    fi
    for tool in python3 git mktemp; do
        command -v "$tool" >/dev/null 2>&1 || { printf 'Required command is missing: %s\n' "$tool" >&2; exit 1; }
    done
    env -i PATH="$PATH" HOME="$HOME" PYTHONNOUSERSITE=1 \
        python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3,10) else "Python 3.10+ is required")'
    work=$(mktemp -d "${TMPDIR:-/tmp}/oc-box-install.XXXXXXXX")
    trap 'rm -rf -- "$work"' EXIT HUP INT TERM
    printf '%s\n' 'Downloading oc-box from https://github.com/groopcd/oc-box (master)...'
    env -i PATH="$PATH" HOME="$work" XDG_CONFIG_HOME="$work/config" \
        GIT_CONFIG_NOSYSTEM=1 GIT_CONFIG_GLOBAL=/dev/null GIT_TERMINAL_PROMPT=0 \
        GIT_ASKPASS=/bin/false LANG=C.UTF-8 \
        git -c credential.helper= -c core.hooksPath=/dev/null \
        -c http.followRedirects=false \
        -c protocol.allow=never -c protocol.https.allow=always \
        clone --quiet --single-branch --branch master \
        https://github.com/groopcd/oc-box.git "$work/source"
    env -i PATH="$PATH" HOME="$HOME" LANG=C.UTF-8 PYTHONNOUSERSITE=1 \
        GIT_CONFIG_NOSYSTEM=1 GIT_CONFIG_GLOBAL=/dev/null \
        python3 -B "$work/source/install.py"
}

# Keep actions in main so a truncated pipeline does not execute a partial script.
main "$@"
