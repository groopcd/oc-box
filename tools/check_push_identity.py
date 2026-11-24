#!/usr/bin/env python3
"""Git pre-push hook checking the raw author and committer emails."""

import subprocess
import sys


IDENTITY_KEY = "ocbox.pushIdentityEmail"


def git(*args):
    return subprocess.check_output(
        ["git", "--no-replace-objects", *args], stderr=subprocess.DEVNULL
    )


def commit_tip(oid):
    if git("cat-file", "-t", f"{oid}^{{}}").strip() != b"commit":
        return None  # Tags can point to trees or blobs as well as commits.
    return git("rev-parse", "--verify", f"{oid}^{{commit}}").strip().decode("ascii")


def outgoing_identities(remote, local_oid, remote_oid):
    tip = commit_tip(local_oid)
    if tip is None:
        return
    revisions = [tip]
    if remote_oid.strip("0"):
        previous = commit_tip(remote_oid)
        if previous is not None:
            revisions.append(f"^{previous}")
    else:
        # A new ref has no old tip. Don't recheck history already tracked
        # from this remote, e.g. when tagging a published release commit.
        revisions.extend(("--not", f"--remotes={remote}"))
    data = git(
        "log",
        "-z",
        "--no-show-signature",
        "--no-decorate",
        "--no-notes",
        "--format=%H%x00%ae%x00%ce",
        *revisions,
        "--",
    )
    if data:
        fields = iter(data[:-1].split(b"\0"))
        yield from zip(fields, fields, fields, strict=True)


def main(remote):
    updates = [
        (local_oid, remote_ref, remote_oid)
        for _, local_oid, remote_ref, remote_oid in map(str.split, sys.stdin)
        if local_oid.strip("0")
    ]
    if not updates:
        return 0
    try:
        allowed = set(
            git("config", "--local", "--null", "--get-all", IDENTITY_KEY).split(b"\0")
        )
    except subprocess.CalledProcessError:
        allowed = set()
    allowed.discard(b"")
    if not allowed:
        print(
            f"pre-push: no allowed email; set {IDENTITY_KEY} in local Git config.",
            file=sys.stderr,
        )
        return 1

    failed = False
    for local_oid, remote_ref, remote_oid in updates:
        for oid, author, committer in outgoing_identities(
            remote, local_oid, remote_oid
        ):
            unexpected = [
                field
                for field, email in (("author", author), ("committer", committer))
                if email not in allowed
            ]
            if unexpected:
                failed = True
                print(
                    f"pre-push: {remote_ref}: {oid[:12].decode('ascii')} has an "
                    f"unapproved {' and '.join(unexpected)} email. Review before pushing.",
                    file=sys.stderr,
                )
    return int(failed)


if __name__ == "__main__":
    try:
        status = main(sys.argv[1])
    except (IndexError, OSError, ValueError, subprocess.CalledProcessError):
        print(
            "pre-push: could not verify commit identities; check local history "
            "and fetch the destination ref before retrying.",
            file=sys.stderr,
        )
        status = 1
    sys.exit(status)
