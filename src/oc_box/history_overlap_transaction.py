"""Strict sequencing for a guarded, create-only history import."""

from dataclasses import dataclass
import json
import re
from typing import Callable

if __package__:
    from .history_overlap_archive import SealedCandidate
    from .history_overlap_order import OverlapRefusal, Route
else:
    from history_overlap_archive import SealedCandidate
    from history_overlap_order import OverlapRefusal, Route


@dataclass(frozen=True)
class FirstImport:
    """Operations on a previously verified and still-admitted destination.

    The launcher supplies all actual native and durable operations. In
    particular, absence is a fresh positive native observation, not an
    inferred empty checkpoint, exit status, or missing private file.
    No callback may discover a new native service, replay an uncertain
    writer, or delete an unknown archive.
    """

    absent: Callable[[Route], bool]
    reserve: Callable[[Route, str], object]
    before_write: Callable[[], None]
    native_import: Callable[[str], None]
    readback: Callable[[Route, str], bool]
    publish_archive: Callable[[SealedCandidate], None]
    publish_checkpoint: Callable[[Route, str], object]
    retire: Callable[[object, object], bool]


@dataclass(frozen=True)
class AtomicFirstImport:
    """Only for an independently qualified native create-only operation.

    The operation starts one fresh private writer, submits only one create,
    and retires it before returning its exact newly-created target. Native
    conflict or duplicate-revision returns must not become acknowledgements.
    """

    reserve: Callable[[Route, str], object]
    before_write: Callable[[], None]
    native_create: Callable[[SealedCandidate, Route], str]
    readback: Callable[[Route, str], bool]
    publish_archive: Callable[[SealedCandidate], None]
    publish_checkpoint: Callable[[Route, str], object]
    retire: Callable[[object, object], bool]


@dataclass(frozen=True)
class FirstPass:
    """Private adapter supplied only after an admitted source/host binding.

    All native operations, including inventory, must remain owned and
    separately admitted by the adapter. This is not a CLI selection or
    a way to turn an ordinary quiet-source lease into a proven domain.
    """

    inventory: Callable
    read: Callable
    begin: Callable
    receive: Callable
    cancel: Callable
    destination: Callable


@dataclass(frozen=True)
class DestinationCreate:
    """Pinned native operations and short publication admission for one target."""

    create: Callable[[SealedCandidate, Route], str]
    readback: Callable[[Route], dict]
    publish_gate: Callable


def native_missing_target(result, target):
    """Strict stock 2.4 single-ID 404 interpretation, not a command runner."""
    if (
        type(target) is not str
        or re.fullmatch(r"ses[A-Za-z0-9_-]{1,253}", target) is None
        or type(result.returncode) is not int
        or result.returncode != 1
        or result.stderr != "HTTP 404 Not Found\n"
        or type(result.stdout) is not str
        or not 1 <= len(result.stdout) <= 4096
    ):
        return False

    def unique(pairs):
        record = dict(pairs)
        if len(record) != len(pairs):
            raise ValueError
        return record

    try:
        body = json.loads(result.stdout, object_pairs_hook=unique)
    except (ValueError, RecursionError):
        return False
    return type(body) is dict and body == {
        "_tag": "SessionNotFoundError",
        "message": "Session not found: " + target,
        "sessionID": target,
    }


def create_candidate(route, candidate, operations):
    """Absolute order: absence, intent, import, independent proof, publication.

    No retries, rollback, or new host work is admitted on any failure. A
    caller must retain the durable attempt if a mutation might have occurred,
    including on failure after native readback and before durable completion.
    """
    if (
        type(route) is not Route
        or type(candidate) is not SealedCandidate
        or type(operations) is not FirstImport
    ):
        raise OverlapRefusal("destination-binding")
    # Verify the bound descriptor before reserving an operation. The caller's
    # native spawn still checks the exact pinned process and stop admission.
    source = candidate.native_path()
    if operations.absent(route) is not True:
        raise OverlapRefusal("destination-not-absent")
    return _ordered_create(
        route, candidate, source, operations, operations.native_import
    )


def create_candidate_once(route, candidate, operations):
    """Use a native create-if-absent contract instead of an earlier lookup."""
    if (
        type(route) is not Route
        or type(candidate) is not SealedCandidate
        or type(operations) is not AtomicFirstImport
    ):
        raise OverlapRefusal("destination-binding")

    def create(_source):
        acknowledged = operations.native_create(candidate, route)
        if type(acknowledged) is not str or acknowledged != route.archive:
            raise OverlapRefusal("destination-acknowledgement")

    return _ordered_create(
        route, candidate, candidate.native_path(), operations, create
    )


def _ordered_create(route, candidate, source, operations, write):
    intent = operations.reserve(route, candidate.digest)
    if intent is None:
        raise OverlapRefusal("destination-intent")
    operations.before_write()
    if candidate.native_path() != source:
        raise OverlapRefusal("candidate-descriptor")
    write(source)
    if operations.readback(route, candidate.digest) is not True:
        raise OverlapRefusal("destination-readback")
    operations.publish_archive(candidate)
    completed = operations.publish_checkpoint(route, candidate.digest)
    if completed is None or operations.retire(intent, completed) is not True:
        raise OverlapRefusal("destination-checkpoint")
    return candidate.digest
