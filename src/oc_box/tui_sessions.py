"""Small, transport-free V2 session domain for the oc-box manager.

``request(method, path, body=None)`` must address an explicitly connected store;
this module never discovers/starts a service, reads a database, exports history,
unlocks credentials or synchronizes mirrors. A catalog's origin/store labels must
remain bound to that connection. Metadata returned to the UI is allowlisted.

Mutations require ``ownership(origin, store, session_ids)`` to return exactly
``"exclusive"`` or ``"process-local"``. The caller must verify its existing owner
lease/connection before returning either; the default refuses. ``process-local``
explicitly acknowledges that native active/job inventories describe only that
server process: native V2 offers no cross-process transaction or compare/delete.
Another process can change global host history after the last check. Never claim
these checks eliminate that race. Unknown ownership, unsupported activity APIs,
and active/unconsumed work are refusals, not invitations to abort work.
"""

from collections import defaultdict
from contextlib import contextmanager
import copy
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import re
import secrets
import stat
import time
from urllib.parse import quote, urlencode


MAX_PAGES = 1000
MAX_SESSIONS = 100000
MAX_CURSOR_BYTES = 16384
MAX_FAVORITES = 10000
MAX_STATE_BYTES = 4 * 1024 * 1024
FAVORITES_LOCK_TIMEOUT = 0.25
PROCESS_LOCAL_WARNING = (
    "Native activity checks cover this server only; another process may change "
    "host sessions between validation and mutation."
)


class CatalogError(RuntimeError):
    """Fixed-category refusal; never incorporates private native exceptions."""

    def __init__(self, reason):
        self.reason = reason
        super().__init__(f"Session operation refused ({reason}).")


def _fail(reason):
    raise CatalogError(reason) from None


def _session_id(value):
    return isinstance(value, str) and re.fullmatch(r"ses[A-Za-z0-9_-]+", value)


def _text(value):
    if not isinstance(value, str):
        return False
    try:
        return (
            bool(value)
            and len(value.encode("utf-8")) <= 4096
            and not any(ord(char) < 32 or ord(char) == 127 for char in value)
        )
    except UnicodeError:
        return False


def _directory(value):
    return _text(value) and value.startswith("/")


def session_identity(origin, store, session_id):
    """Stable identity; a same-ID source and mirror are different selections."""
    if not _text(origin) or not _text(store) or not _session_id(session_id):
        _fail("invalid-identity")
    return {"origin": origin, "store": store, "id": session_id}


def normalize_session(value):
    """Copy only native fields needed for identity, grouping and confirmation."""
    if not isinstance(value, dict) or not _session_id(value.get("id")):
        _fail("invalid-session")
    location, times = value.get("location"), value.get("time")
    if (
        not _text(value.get("projectID"))
        or not isinstance(location, dict)
        or not _directory(location.get("directory"))
        or not isinstance(times, dict)
        or not {"created", "updated"}.issubset(times)
    ):
        _fail("invalid-session")
    clean_times = {}
    for key in ("created", "updated", "busy", "idle", "viewed", "archived"):
        if key in times:
            number = times[key]
            try:
                valid_number = type(number) in (int, float) and math.isfinite(number)
            except OverflowError:
                valid_number = False
            if not valid_number:
                _fail("invalid-session")
            clean_times[key] = number
    result = {
        "id": value["id"],
        "projectID": value["projectID"],
        "location": {"directory": location["directory"]},
        "time": clean_times,
    }
    if "title" in value:
        if not isinstance(value["title"], str):
            _fail("invalid-session")
        result["title"] = value["title"]
    if "parentID" in value:
        if not _session_id(value["parentID"]) or value["parentID"] == value["id"]:
            _fail("invalid-session")
        result["parentID"] = value["parentID"]
    return result


def _call(request, method, path, body=None):
    try:
        return request(method, path) if body is None else request(method, path, body)
    except Exception:
        _fail("request-failed")


def _check_cancelled(cancelled):
    if cancelled is not None and cancelled():
        _fail("cancelled")


def list_sessions(request, *, directory=None, cancelled=None):
    """Return a complete inventory or raise; no partial-result success.

    Omitted archived/parentID filters intentionally include archived and child
    sessions. A short *or empty* page is not completion if it has a next cursor.
    Cursors are opaque and URL encoded; missing next is invalid, not exhaustion.
    """
    if directory is not None and not _directory(directory):
        _fail("invalid-directory")
    query = [("order", "asc"), ("limit", "100")]
    if directory is not None:
        query.append(("directory", directory))
    route = "/api/session?" + urlencode(query, quote_via=quote)
    values, identities, cursors = [], set(), set()
    cursor = None
    for _ in range(MAX_PAGES):
        _check_cancelled(cancelled)
        page = _call(
            request,
            "GET",
            route + ("&cursor=" + quote(cursor, safe="") if cursor else ""),
        )
        _check_cancelled(cancelled)
        if (
            not isinstance(page, dict)
            or not isinstance(page.get("data"), list)
            or not isinstance(page.get("cursor"), dict)
            or "next" not in page["cursor"]
        ):
            _fail("invalid-inventory")
        for raw in page["data"]:
            item = normalize_session(raw)
            if item["id"] in identities:
                _fail("duplicate-session")
            if directory is not None and item["location"]["directory"] != directory:
                _fail("inconsistent-directory")
            identities.add(item["id"])
            values.append(item)
            if len(values) > MAX_SESSIONS:
                _fail("inventory-limit")
        cursor = page["cursor"]["next"]
        if cursor is None:
            return values
        if not isinstance(cursor, str) or not cursor or cursor in cursors:
            _fail("invalid-cursor")
        try:
            if len(cursor.encode("utf-8")) > MAX_CURSOR_BYTES:
                _fail("invalid-cursor")
        except UnicodeError:
            _fail("invalid-cursor")
        cursors.add(cursor)
    _fail("inventory-limit")


def _descendants(values, session_id):
    by_id = {item["id"]: item for item in values}
    if session_id not in by_id:
        _fail("session-missing")
    children = defaultdict(list)
    for item in values:
        if item.get("parentID"):
            children[item["parentID"]].append(item["id"])
    pending, seen, result = [session_id], set(), []
    while pending:
        ident = pending.pop()
        if ident in seen:
            _fail("invalid-session-tree")
        seen.add(ident)
        result.append(by_id[ident])
        pending.extend(sorted(children[ident], reverse=True))
    return result


class SessionCatalog:
    """One explicit connection and origin; no operation follows a mirror link.

    ``list`` establishes selection snapshots. Archive requires that snapshot;
    delete preparation may also be used directly, establishing its own snapshot.
    ``managed_ids`` are verified host archive copies, not title/prefix matches.
    Native children on any managed copy refuse recursive deletion outright.
    Delete previews are plain dictionaries: origin, store, id, sessions (selected
    root first, all descendants), descendant_count, ownership, warning and token.
    Tokens are local to this catalog, single-use, and never native metadata.
    """

    def __init__(self, request, origin, store, *, ownership=None, managed_ids=()):
        session_identity(origin, store, "ses_validation")
        managed_ids = tuple(managed_ids)
        if any(not _session_id(ident) for ident in managed_ids):
            _fail("invalid-identity")
        self.request, self.origin, self.store = request, origin, store
        self._binding = (request, origin, store)
        self.ownership = ownership
        self.managed_ids = frozenset(managed_ids)
        self._snapshots, self._previews = {}, {}

    def _validate_binding(self):
        request, origin, store = self._binding
        if self.request is not request or (self.origin, self.store) != (origin, store):
            _fail("origin-changed")

    def list(self, directory=None, *, cancelled=None):
        self._validate_binding()
        values = list_sessions(self.request, directory=directory, cancelled=cancelled)
        # A failed/cancelled refresh never replaces the last complete snapshot.
        self._snapshots = {item["id"]: copy.deepcopy(item) for item in values}
        self._previews.clear()
        return values

    def _get(self, ident):
        self._validate_binding()
        if not _session_id(ident):
            _fail("invalid-identity")
        response = _call(self.request, "GET", "/api/session/" + quote(ident, safe=""))
        value = normalize_session(
            response.get("data") if isinstance(response, dict) else None
        )
        if value["id"] != ident:
            _fail("identity-changed")
        return value

    def _unchanged(self, expected):
        if self._get(expected["id"]) != expected:
            _fail("snapshot-changed")

    def _idle(self, identities):
        self._validate_binding()
        if self.ownership is None:
            _fail("ownership-unknown")
        try:
            ownership = self.ownership(self.origin, self.store, tuple(identities))
        except Exception:
            _fail("ownership-unknown")
        if ownership not in ("exclusive", "process-local"):
            _fail("ownership-unknown")
        self._validate_binding()
        # Two complete quiet pairs catch the completed-job -> foreground handoff.
        for _ in range(2):
            response = _call(self.request, "GET", "/api/session/active")
            active = response.get("data") if isinstance(response, dict) else None
            if not isinstance(active, dict) or any(
                not _session_id(ident)
                or not isinstance(state, dict)
                or state.get("type") != "running"
                for ident, state in active.items()
            ):
                _fail("invalid-activity")
            response = _call(self.request, "GET", "/api/experimental/job")
            jobs = response.get("data") if isinstance(response, dict) else None
            if not isinstance(jobs, list) or any(
                not isinstance(job, dict)
                or not _text(job.get("id"))
                or not _session_id(job.get("sessionID"))
                or job.get("kind") not in ("shell", "subagent", "image")
                or job.get("status")
                not in ("running", "completed", "error", "cancelled")
                for job in jobs
            ):
                _fail("invalid-jobs")
            # Refuse store-wide activity: another root's subagent/job can affect
            # descendant ownership, and a quiet subset is not a quiet store.
            if active or jobs:
                _fail("session-active")
        self._validate_binding()
        return ownership

    def archive(self, session_id, archived):
        """PATCH only the selected native session; 204/None is success."""
        self._validate_binding()
        if type(archived) is not bool:
            _fail("invalid-archive-state")
        session_identity(self.origin, self.store, session_id)
        expected = self._snapshots.get(session_id)
        if expected is None:
            _fail("snapshot-required")
        self._unchanged(expected)
        self._idle([session_id])
        self._unchanged(expected)
        self._validate_binding()
        result = _call(
            self.request,
            "PATCH",
            "/api/session/" + quote(session_id, safe=""),
            {"archived": archived},
        )
        self._snapshots.pop(session_id, None)
        self._previews.clear()
        if result is not None:
            _fail("invalid-mutation-response")
        return None

    def _delete_snapshot(self, session_id):
        # No directory filter: native DELETE follows children across locations.
        values = list_sessions(self.request)
        selected = _descendants(values, session_id)
        for item in selected:
            self._unchanged(item)
        if (
            any(item["id"] in self.managed_ids for item in selected)
            and len(selected) > 1
        ):
            _fail("managed-archive-has-children")
        ownership = self._idle([item["id"] for item in selected])
        self._no_running_terminals(selected)
        return selected, ownership

    def _no_running_terminals(self, selected):
        # History synchronization deliberately permits idle persistent PTYs.
        # Deleting their owning session is different: never implicitly interrupt
        # a shell, including one not represented by foreground/job inventories.
        for item in selected:
            response = _call(
                self.request,
                "GET",
                "/api/experimental/session/" + quote(item["id"], safe="") + "/terminal",
            )
            terminals = response.get("data") if isinstance(response, dict) else None
            if not isinstance(terminals, list) or any(
                not isinstance(terminal, dict)
                or terminal.get("sessionID") != item["id"]
                or terminal.get("status") not in ("running", "exited")
                for terminal in terminals
            ):
                _fail("invalid-terminals")
            if any(terminal["status"] == "running" for terminal in terminals):
                _fail("session-terminal-running")

    def prepare_delete(self, session_id):
        self._validate_binding()
        session_identity(self.origin, self.store, session_id)
        selected, ownership = self._delete_snapshot(session_id)
        previous = self._snapshots.get(session_id)
        if previous is not None and previous != selected[0]:
            _fail("snapshot-changed")
        preview = {
            **session_identity(self.origin, self.store, session_id),
            "sessions": selected,
            "descendant_count": len(selected) - 1,
            "ownership": ownership,
            "warning": PROCESS_LOCAL_WARNING if ownership == "process-local" else "",
            "token": secrets.token_hex(24),
        }
        self._previews = {preview["token"]: copy.deepcopy(preview)}
        return preview

    def delete(self, preview):
        """Revalidate confirmed origin, full tree, identity and quiet ownership."""
        self._validate_binding()
        if not isinstance(preview, dict) or not isinstance(preview.get("token"), str):
            _fail("invalid-preview")
        expected = self._previews.get(preview["token"])
        if expected is None or expected != preview:
            _fail("invalid-preview")
        if preview["origin"] != self.origin or preview["store"] != self.store:
            _fail("origin-changed")
        selected, ownership = self._delete_snapshot(preview["id"])
        if selected != preview["sessions"] or ownership != preview["ownership"]:
            _fail("snapshot-changed")
        # A second full tree observation also detects newly linked children that
        # appeared during identity/activity reads. Native still has a final race.
        if _descendants(list_sessions(self.request), preview["id"]) != selected:
            _fail("snapshot-changed")
        for item in selected:
            self._unchanged(item)
        if self._idle([item["id"] for item in selected]) != preview["ownership"]:
            _fail("snapshot-changed")
        self._no_running_terminals(selected)
        self._validate_binding()
        result = _call(
            self.request, "DELETE", "/api/session/" + quote(preview["id"], safe="")
        )
        self._previews.clear()
        for item in selected:
            self._snapshots.pop(item["id"], None)
        if result is not None:
            _fail("invalid-mutation-response")
        return None


def archive_associations(
    registry, state, provenance, *, source_origin, source_store, host_store, native=True
):
    """Pure consistency check of *private, caller-verified* archive provenance.

    ``registry`` is one workspace.json, ``state`` its per-major sessions state,
    and ``provenance`` maps archive IDs to saved local history **info** objects.
    The caller validates private file ownership/paths before loading them. This
    helper performs no transcript reads, trusts neither UI titles nor prefixes,
    and does not establish filesystem trust for arbitrary supplied dictionaries.
    A mapping AND matching saved provenance AND the expected namespace must
    agree. Missing provenance leaves a copy unassociated, not guessed.
    Relationships come only from the verified saved archive, never host metadata.
    """
    if (
        not isinstance(registry, dict)
        or not isinstance(registry.get("id"), str)
        or not re.fullmatch(r"[0-9a-f]{32}", registry["id"])
        or not _directory(registry.get("project"))
        or not isinstance(state, dict)
        or not isinstance(state.get("host_sessions", {}), dict)
        or not isinstance(provenance, dict)
        or type(native) is not bool
    ):
        _fail("invalid-provenance")
    mapping = state.get("host_sessions", {})
    inverse = {}
    for source, host in mapping.items():
        if not _session_id(source) or not _session_id(host) or host in inverse:
            _fail("invalid-provenance")
        inverse[host] = source
    namespace = registry["id"] + ("-v2" if native else "")
    prefix = "ses_" + hashlib.sha256(namespace.encode()).hexdigest()[:12] + "_"
    result, parents = [], {}
    for source, host in mapping.items():
        saved = provenance.get(host)
        if saved is None:
            continue
        directory = (
            saved.get("location", {}).get("directory")
            if native
            and isinstance(saved, dict)
            and isinstance(saved.get("location"), dict)
            else saved.get("directory")
            if isinstance(saved, dict)
            else None
        )
        if (
            not isinstance(saved, dict)
            or saved.get("id") != host
            or directory != registry["project"]
            or host != prefix + source.split("_", 1)[-1]
        ):
            _fail("invalid-provenance")
        row = {
            "source": session_identity(source_origin, source_store, source),
            "archive": session_identity("host", host_store, host),
            "project": registry["project"],
        }
        metadata = saved.get("metadata", {})
        relationship = (
            metadata.get("oc-box.archive") if isinstance(metadata, dict) else None
        )
        if (
            isinstance(relationship, dict)
            and type(relationship.get("version")) is int
            and relationship["version"] == 1
        ):
            parent = relationship.get("parentID")
            if _session_id(parent) and parent in inverse and parent != host:
                parents[host] = parent
        result.append(row)
    verified = {row["archive"]["id"] for row in result}
    for row in result:
        parent = parents.get(row["archive"]["id"])
        if parent in verified:
            row["parent"] = session_identity("host", host_store, parent)
    return result


def sessions_for_project(values, directory, *, origin, store, associations=()):
    """Select native-location sessions and verified archive copies for a project.

    ``associations`` must come from ``archive_associations``; project attribution
    never rewrites the native location or follows the source endpoint for actions.
    No title, namespace prefix or host-supplied metadata is project authority.
    """
    session_identity(origin, store, "ses_validation")
    if not _directory(directory):
        _fail("invalid-directory")
    associated = set()
    for relationship in associations:
        if not isinstance(relationship, dict):
            _fail("invalid-provenance")
        identity = relationship.get("archive")
        if not isinstance(identity, dict) or set(identity) != {"origin", "store", "id"}:
            _fail("invalid-provenance")
        session_identity(identity["origin"], identity["store"], identity["id"])
        if (
            relationship.get("project") == directory
            and identity["origin"] == origin
            and identity["store"] == store
        ):
            associated.add(identity["id"])
    result = []
    for raw in values:
        item = normalize_session(raw)
        if item["location"]["directory"] == directory or item["id"] in associated:
            result.append(item)
    return result


class FavoriteStore:
    """Private UI-only favorites; independent of workspace lifecycle locks.

    root must be a dedicated private directory (0700, owned by this user). Files
    are 0600, regular, singly linked, and opened without following symlinks.
    Every read/modify/write uses this store's own flock and atomic replace.
    Corrupt state refuses rather than silently dropping someone's favorites.
    """

    def __init__(self, root):
        self.root = Path(root)

    @staticmethod
    def _private(info, *, directory=False):
        kind = stat.S_ISDIR if directory else stat.S_ISREG
        return (
            kind(info.st_mode)
            and info.st_uid == os.getuid()
            and stat.S_IMODE(info.st_mode) == (0o700 if directory else 0o600)
            and (directory or info.st_nlink == 1)
        )

    @contextmanager
    def _locked(self, *, write=False):
        root_fd = lock_fd = None
        try:
            self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
            root_fd = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            if not self._private(os.fstat(root_fd), directory=True):
                _fail("unsafe-favorites-state")
            lock_fd = os.open(
                "favorites.lock",
                os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK,
                0o600,
                dir_fd=root_fd,
            )
            if not self._private(os.fstat(lock_fd)):
                _fail("unsafe-favorites-state")
            deadline = time.monotonic() + FAVORITES_LOCK_TIMEOUT
            operation = fcntl.LOCK_EX if write else fcntl.LOCK_SH
            while True:
                try:
                    fcntl.flock(lock_fd, operation | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        _fail("favorites-busy")
                    time.sleep(0.005)
            yield root_fd
        except (OSError, ValueError, UnicodeError, RecursionError):
            _fail("favorites-state-failed")
        finally:
            if lock_fd is not None:
                os.close(lock_fd)
            if root_fd is not None:
                os.close(root_fd)

    def _read(self, root_fd):
        try:
            fd = os.open(
                "favorites.json",
                os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                dir_fd=root_fd,
            )
        except FileNotFoundError:
            return []
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            if not self._private(info) or info.st_size > MAX_STATE_BYTES:
                _fail("unsafe-favorites-state")
            try:
                state = json.loads(stream.read(MAX_STATE_BYTES + 1))
            except (ValueError, UnicodeError, RecursionError):
                _fail("invalid-favorites-state")
        if (
            not isinstance(state, dict)
            or type(state.get("version")) is not int
            or state.get("version") != 1
            or set(state) != {"version", "favorites"}
            or not isinstance(state["favorites"], list)
            or len(state["favorites"]) > MAX_FAVORITES
        ):
            _fail("invalid-favorites-state")
        result, seen = [], set()
        for value in state["favorites"]:
            if not isinstance(value, dict) or set(value) != {"origin", "store", "id"}:
                _fail("invalid-favorites-state")
            identity = session_identity(value["origin"], value["store"], value["id"])
            key = tuple(identity.values())
            if key in seen:
                _fail("invalid-favorites-state")
            seen.add(key)
            result.append(identity)
        return result

    def list(self):
        with self._locked() as root_fd:
            return self._read(root_fd)

    def contains(self, origin, store, session_id):
        return session_identity(origin, store, session_id) in self.list()

    def toggle(self, origin, store, session_id):
        identity = session_identity(origin, store, session_id)
        with self._locked(write=True) as root_fd:
            values = self._read(root_fd)
            enabled = identity not in values
            if enabled:
                if len(values) >= MAX_FAVORITES:
                    _fail("favorites-limit")
                values.append(identity)
            else:
                values.remove(identity)
            payload = json.dumps(
                {"version": 1, "favorites": values}, ensure_ascii=True
            ).encode()
            if len(payload) > MAX_STATE_BYTES:
                _fail("favorites-limit")
            temporary = ".favorites-" + secrets.token_hex(16)
            fd = os.open(
                temporary,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o600,
                dir_fd=root_fd,
            )
            try:
                with os.fdopen(fd, "wb") as stream:
                    stream.write(payload)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(
                    temporary, "favorites.json", src_dir_fd=root_fd, dst_dir_fd=root_fd
                )
                os.fsync(root_fd)
            finally:
                try:
                    os.unlink(temporary, dir_fd=root_fd)
                except FileNotFoundError:
                    pass
        return enabled
