#!/bin/sh
# GPL-3.0-or-later. Public bootstrap: no sudo, tokens, global Git config, or hooks.
set -eu

main() {
    channel=
    selected_channel=
    with_vault=false
    with_tui=false
    for argument in "$@"; do
        case "$argument" in
            --stable|--devel)
                if [ -n "$selected_channel" ] && [ "$selected_channel" != "$argument" ]; then
                    printf '%s\n' '--stable and --devel are mutually exclusive.' >&2
                    exit 2
                fi
                selected_channel=$argument
                channel=${argument#--}
                ;;
            --with-vault) with_vault=true ;;
            --with-tui) with_tui=true ;;
            *)
                printf '%s\n' 'Usage: sh install.sh [--stable|--devel] [--with-vault] [--with-tui (if supported by the selected release)]' >&2
                exit 2
                ;;
        esac
    done
    if [ "$(id -u)" = 0 ]; then
        printf '%s\n' 'Run the oc-box installer as your regular host user, without sudo.' >&2
        exit 1
    fi
    for tool in python3 git mktemp; do
        command -v "$tool" >/dev/null 2>&1 || { printf 'Required command is missing: %s\n' "$tool" >&2; exit 1; }
    done
    env -i PATH="$PATH" HOME="$HOME" PYTHONNOUSERSITE=1 \
        python3 -I -c 'import sys; sys.exit(0 if sys.version_info >= (3,10) else "Python 3.10+ is required")'
    work=$(mktemp -d "${TMPDIR:-/tmp}/oc-box-install.XXXXXXXX")
    trap 'rm -rf -- "$work"' EXIT HUP INT TERM
    # There is no trusted downloaded helper yet. Keep discovery stdlib-only,
    # anonymous, bounded and consistent with the installer's channel grammar.
    selection=$(env -i PATH="$PATH" HOME="$work" PYTHONNOUSERSITE=1 \
        python3 -I - "$channel" "${XDG_CONFIG_HOME-"$HOME/.config"}/oc-box/config.json" <<'PY'
import json
from pathlib import Path
import re
import sys
from urllib import error, request

ENDPOINT = "https://api.github.com/repos/groopcd/oc-box/releases"
NUMBER = r"(0|[1-9][0-9]*)"
FINAL = re.compile(r"v" + NUMBER + r"\." + NUMBER + r"\." + NUMBER)
DEVEL = re.compile(FINAL.pattern + r"\.dev0")
LIMIT = 4 * 1024 * 1024


class Refusal(Exception):
    pass


class NoRedirect(request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise Refusal("Release discovery refused an unexpected redirect.")


def selected_channel(requested, filename):
    target = Path(filename)
    if target.is_symlink():
        raise Refusal("The oc-box config must not be a symlink; nothing was installed.")
    try:
        if target.exists():
            with target.open("rb") as stream:
                body = stream.read(1024 * 1024 + 1)
            if len(body) > 1024 * 1024:
                raise ValueError()
            settings = json.loads(body)
        else:
            settings = {}
        if not isinstance(settings, dict):
            raise ValueError()
        saved = settings.get("update_channel", "stable")
        if not isinstance(saved, str) or saved not in ("stable", "devel"):
            raise ValueError()
    except (OSError, ValueError):
        raise Refusal("Cannot validate oc-box channel preferences; nothing was installed.") from None
    return requested or saved


def fetch(url):
    opener = request.build_opener(request.ProxyHandler({}), NoRedirect())
    with opener.open(
        request.Request(
            url,
            headers={"Accept": "application/json", "User-Agent": "oc-box-install"},
        ),
        timeout=15,
    ) as response:
        body = response.read(LIMIT + 1)
    if len(body) > LIMIT:
        raise Refusal("Release discovery exceeded its response limit.")
    return json.loads(body)


def discover(channel):
    if channel == "stable":
        record = fetch(ENDPOINT + "/latest")
        if (
            not isinstance(record, dict)
            or record.get("draft") is not False
            or record.get("prerelease") is not False
            or not isinstance(record.get("tag_name"), str)
            or not FINAL.fullmatch(record["tag_name"])
        ):
            raise Refusal("GitHub did not return a published stable oc-box release.")
        return record["tag_name"][1:]
    selected = None
    for page in range(1, 11):
        records = fetch(ENDPOINT + f"?per_page=100&page={page}")
        if not isinstance(records, list) or len(records) > 100:
            raise Refusal("Unexpected GitHub release response.")
        for record in records:
            if not isinstance(record, dict):
                raise Refusal("Unexpected GitHub release response.")
            tag = record.get("tag_name")
            match = DEVEL.fullmatch(tag) if isinstance(tag, str) else None
            if (
                record.get("draft") is False
                and record.get("prerelease") is True
                and match
            ):
                ordering = tuple(map(int, match.groups()))
                if selected is None or ordering > selected[0]:
                    selected = (ordering, tag[1:])
        if len(records) < 100:
            if selected is None:
                raise Refusal("No published development oc-box release was found.")
            return selected[1]
    raise Refusal("GitHub release inventory exceeded its pagination limit.")


try:
    channel = selected_channel(sys.argv[1], sys.argv[2])
    print(channel + " " + discover(channel))
except Refusal as exc:
    sys.exit(str(exc))
except (OSError, ValueError, error.URLError):
    sys.exit("Public release discovery failed; nothing was installed.")
PY
    )
    channel=${selection%% *}
    version=${selection#* }
    case "$channel" in
        stable) branch=master ;;
        devel) branch=devel ;;
    esac
    tag=v$version
    upstream=https://github.com/groopcd/oc-box.git
    public_git() {
        env -i PATH="$PATH" HOME="$work" XDG_CONFIG_HOME="$work/config" \
            GIT_CONFIG_NOSYSTEM=1 GIT_CONFIG_GLOBAL=/dev/null GIT_TERMINAL_PROMPT=0 \
            GIT_ASKPASS=/bin/false LANG=C.UTF-8 \
            git -c credential.helper= -c core.hooksPath=/dev/null \
            -c http.followRedirects=false \
            -c protocol.allow=never -c protocol.https.allow=always "$@"
    }
    printf 'Downloading published oc-box %s (%s)...\n' "$tag" "$branch"
    public_git clone --quiet --single-branch --no-tags --branch "$branch" \
        "$upstream" "$work/source"
    public_git -C "$work/source" fetch --quiet --no-tags "$upstream" \
        "refs/tags/$tag:refs/tags/$tag"
    commit=$(public_git -C "$work/source" rev-parse --verify "refs/tags/$tag^{commit}")
    case "$commit" in
        ''|*[!0123456789abcdef]*)
            printf '%s\n' 'Downloaded release commit ID is invalid; nothing was installed.' >&2
            exit 1
            ;;
    esac
    if [ "${#commit}" -ne 40 ]; then
        printf '%s\n' 'Downloaded release commit ID is invalid; nothing was installed.' >&2
        exit 1
    fi
    if ! public_git -C "$work/source" merge-base --is-ancestor \
        "$commit" "refs/remotes/origin/$branch"; then
        printf 'Published release %s is not on the required %s branch; nothing was installed.\n' "$tag" "$branch" >&2
        exit 1
    fi
    public_git -C "$work/source" checkout --quiet --detach "$commit"
    if [ "$with_tui" = true ]; then
        # Features belong to the verified release, not its update channel.
        # Inspect parser literals without importing or executing installer code.
        env -i PATH="$PATH" HOME="$work" PYTHONNOUSERSITE=1 \
            python3 -I - "$work/source" <<'PY'
import ast
from pathlib import Path
import sys

source = Path(sys.argv[1])
implementation = source / "src/oc_box/install.py"
if not implementation.exists():
    implementation = source / "install.py"
try:
    tree = ast.parse(implementation.read_text(encoding="utf-8"))
except (OSError, SyntaxError, UnicodeError):
    sys.exit("Cannot inspect the selected release installer; nothing was installed.")
if not any(
    isinstance(node, ast.Constant) and node.value == "--with-tui"
    for node in ast.walk(tree)
):
    sys.exit(
        "The selected release installer does not support --with-tui; "
        "nothing was installed."
    )
PY
    fi
    set -- "--$channel" --release-version "$version"
    if [ "$with_vault" = true ]; then set -- "$@" --with-vault; fi
    if [ "$with_tui" = true ]; then set -- "$@" --with-tui; fi
    env -i PATH="$PATH" HOME="$HOME" LANG=C.UTF-8 PYTHONNOUSERSITE=1 \
        XDG_CONFIG_HOME="${XDG_CONFIG_HOME-"$HOME/.config"}" \
        GIT_CONFIG_NOSYSTEM=1 GIT_CONFIG_GLOBAL=/dev/null \
        python3 -B "$work/source/install.py" "$@"
}

# Keep actions in main so a truncated pipeline does not execute a partial script.
main "$@"
