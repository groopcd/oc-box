"""Read-only exclusion check for the synthetic host-sharing backup fixture.

Usage: age --decrypt ... | python -I -B tools/sharing_archive_scan.py tar|oci
The caller MUST check the producer's exit status too (for example, pipefail).
Reading EOF is necessary, but cannot itself establish age authentication.

No input bytes, names, exceptions, counts, or digests are printed. Success is
{"external_tree_absent":true}; refusal is the same fixed boolean set to false,
with a fixed category on stderr and exit 1. This is not a general secret scan:
deliberately disclosed text/images in native history are allowed.
"""

import bz2
import hashlib
import json
import lzma
import math
import re
import sys
import tarfile
import zlib


CHUNK_BYTES = 64 * 1024
MAX_TOTAL_BYTES = 256 * 1024**3  # Cumulative input and expanded layer bytes.
MAX_MEMBER_BYTES = 64 * 1024**3
MAX_MEMBERS = 1_000_000  # Includes extension headers across all nested tars.
MAX_NAME_BYTES = 4096
MAX_PAX_BYTES = 64 * 1024
MAX_PAX_FIELDS = 128
MAX_EXTENSION_HEADERS = 16
MAX_JSON_BYTES = 2 * 1024**2
MAX_JSON_DEPTH = 64
MAX_BLOBS = 65_536
MAX_DESCRIPTORS = 65_536
XZ_MEMORY_BYTES = 64 * 1024**2

FORBIDDEN_COMPONENTS = frozenset(
    ("share-input-a", "share-input-b", "UNREAD_EXTERNAL_TREE_SENTINEL")
)
FORBIDDEN_CONTENT = b"SHARING_NEVER_READ_CONTENT_"
SUCCESS = '{"external_tree_absent":true}\n'
REFUSAL = '{"external_tree_absent":false}\n'
DIGEST = re.compile(r"sha256:([0-9a-f]{64})\Z")
BLOB_PATH = re.compile(r"blobs/sha256/([0-9a-f]{64})\Z")
INDEX_MEDIA = frozenset(
    (
        "application/vnd.oci.image.index.v1+json",
        "application/vnd.docker.distribution.manifest.list.v2+json",
    )
)
MANIFEST_MEDIA = frozenset(
    (
        "application/vnd.oci.image.manifest.v1+json",
        "application/vnd.docker.distribution.manifest.v2+json",
    )
)
LAYER_MEDIA = {
    "application/vnd.oci.image.layer.v1.tar": "tar",
    "application/vnd.oci.image.layer.v1.tar+gzip": "gzip",
    "application/vnd.oci.image.layer.v1.tar+bzip2": "bz2",
    "application/vnd.oci.image.layer.v1.tar+bz2": "bz2",
    "application/vnd.oci.image.layer.v1.tar+xz": "xz",
    "application/vnd.oci.image.layer.nondistributable.v1.tar": "tar",
    "application/vnd.oci.image.layer.nondistributable.v1.tar+gzip": "gzip",
    "application/vnd.docker.image.rootfs.diff.tar": "tar",
    "application/vnd.docker.image.rootfs.diff.tar.gzip": "gzip",
    "application/vnd.docker.image.rootfs.foreign.diff.tar.gzip": "gzip",
}


class ScanError(Exception):
    """Only literal, non-sensitive categories may cross the CLI boundary."""


class State:
    def __init__(self):
        self.bytes = 0
        self.members = 0
        self.violation = None
        self.blobs = {}
        self.references = []

    def account(self, size):
        self.bytes += size
        if self.bytes > MAX_TOTAL_BYTES:
            raise ScanError("resource-limit")

    def member(self):
        self.members += 1
        if self.members > MAX_MEMBERS:
            raise ScanError("resource-limit")

    def path(self, name):
        if len(name.encode("utf-8", "surrogateescape")) > MAX_NAME_BYTES:
            raise ScanError("resource-limit")
        if "\0" in name:
            raise ScanError("malformed-tar")
        if FORBIDDEN_COMPONENTS.intersection(name.split("/")):
            self.violation = self.violation or "forbidden-path"


class Reader:
    """Bound reads, optionally accounting, hashing, and checking raw content."""

    def __init__(self, source, state, *, account=False, inspect=False, digest=False):
        self.source = source
        self.state = state
        self.account = account
        self.inspect = inspect
        self.hash = hashlib.sha256() if digest else None
        self.tail = b""

    def read(self, size):
        if size < 0:
            raise ScanError("resource-limit")
        request = min(size, CHUNK_BYTES)
        if self.account:
            if self.state.bytes > MAX_TOTAL_BYTES:
                raise ScanError("resource-limit")
            # At most one extra byte is needed to distinguish EOF at the cap
            # from an over-budget stream. Never overshoot by a whole chunk.
            request = min(request, MAX_TOTAL_BYTES - self.state.bytes + 1)
        data = self.source.read(request)
        if self.account:
            self.state.account(len(data))
        if self.hash is not None:
            self.hash.update(data)
        if self.inspect and data:
            combined = self.tail + data
            if FORBIDDEN_CONTENT in combined:
                self.state.violation = self.state.violation or "forbidden-content"
            self.tail = combined[-(len(FORBIDDEN_CONTENT) - 1) :]
        return data


class PrefixReader:
    def __init__(self, prefix, source):
        self.prefix = prefix
        self.source = source

    def read(self, size):
        if self.prefix:
            result, self.prefix = self.prefix[:size], self.prefix[size:]
            return result
        return self.source.read(size)


class MemberReader:
    """The current regular member's bytes, without any extraction API."""

    def __init__(self, archive, member):
        self.source = archive.fileobj  # tarfile's in-memory forward-only relay.
        self.remaining = member.size
        if self.source.tell() != member.offset_data:
            raise ScanError("malformed-tar")

    def read(self, size):
        size = min(size, self.remaining, CHUNK_BYTES)
        if not size:
            return b""
        data = self.source.read(size)
        if len(data) != size:
            raise ScanError("malformed-tar")
        self.remaining -= len(data)
        return data


class DecompressReader:
    """Bounded stdlib decompression, including checksum/EOF and trailing checks.

    tarfile's r|* decompressor is deliberately not used: some Python versions
    expand a whole compressed input chunk without an output allocation bound.
    Concatenated streams are supported; unknown suffixes are never ignored.
    """

    def __init__(self, source, kind):
        self.source = source
        self.kind = kind
        self.pending = b""
        self.decoder = None

    def new_decoder(self):
        if self.kind == "gzip":
            return zlib.decompressobj(31)
        if self.kind == "bz2":
            return bz2.BZ2Decompressor()
        return lzma.LZMADecompressor(memlimit=XZ_MEMORY_BYTES)

    def read(self, size):
        size = min(size, CHUNK_BYTES)
        if not size:
            return b""
        while True:
            if self.decoder is None:
                if not self.pending:
                    self.pending = self.source.read(CHUNK_BYTES)
                if not self.pending:
                    return b""
                self.decoder = self.new_decoder()
            needs_input = (
                not self.decoder.unconsumed_tail
                if self.kind == "gzip"
                else self.decoder.needs_input
            )
            if needs_input and not self.pending:
                self.pending = self.source.read(CHUNK_BYTES)
                if not self.pending:
                    raise ScanError("malformed-compression")
            data, self.pending = self.pending, b""
            if self.kind == "gzip" and self.decoder.unconsumed_tail:
                data = self.decoder.unconsumed_tail
            try:
                result = self.decoder.decompress(data, size)
            except (OSError, EOFError, zlib.error, lzma.LZMAError):
                raise ScanError("malformed-compression") from None
            if self.decoder.eof:
                self.pending = self.decoder.unused_data
                self.decoder = None
            if result:
                return result


def drain(source):
    while source.read(CHUNK_BYTES):
        pass


def exact(source, size):
    pieces = []
    while size:
        data = source.read(min(size, CHUNK_BYTES))
        if not data:
            raise ScanError("malformed-tar")
        pieces.append(data)
        size -= len(data)
    return b"".join(pieces)


def opaque_xattr_key(key):
    """Only the established SCHILY xattr-value convention is binary-valued.

    These values are metadata, never pathname/size overrides. Keep their keys
    and lossless values in the normal mapping for duplicate and field budgets.
    """
    prefix = "SCHILY.xattr."
    return (
        key.startswith(prefix)
        and len(key) > len(prefix)
        and not any(char in key for char in ("\0", "\n", "\r", "="))
    )


def pax_fields(payload):
    """Strict framing before tarfile can allocate or silently ignore PAX data."""
    result = {}
    offset = 0
    while offset < len(payload):
        space = payload.find(b" ", offset, offset + 12)
        if space < 0 or not payload[offset:space].isdigit():
            raise ScanError("malformed-pax")
        length = int(payload[offset:space])
        end = offset + length
        if length < 5 or end > len(payload) or payload[end - 1 : end] != b"\n":
            raise ScanError("malformed-pax")
        field, equals, value = payload[space + 1 : end - 1].partition(b"=")
        if (
            not equals
            or not field
            or any(char in field for char in (b"\0", b"\n", b"\r"))
        ):
            raise ScanError("malformed-pax")
        try:
            key = field.decode("utf-8")
            if opaque_xattr_key(key):
                # Container archive writers carry raw xattr bytes here. Python
                # ignores these keys semantically; preserve bytes reversibly,
                # not with replacement/ignore decoding or a blanket PAX charset.
                text = value.decode("utf-8", "surrogateescape")
            else:
                if b"\0" in value:
                    raise ScanError("malformed-pax")
                text = value.decode("utf-8")
        except UnicodeError:
            raise ScanError("malformed-pax") from None
        if key in result or key.startswith(("GNU.sparse", "SCHILY.realsize")):
            raise ScanError("unsupported-tar")
        if key == "hdrcharset" and text != "ISO-IR 10646 2000 UTF-8":
            raise ScanError("unsupported-tar")
        if key == "size":
            if not re.fullmatch(r"[0-9]{1,20}", text):
                raise ScanError("malformed-pax")
            if int(text) > MAX_MEMBER_BYTES:
                raise ScanError("resource-limit")
        if key in ("uid", "gid") and not re.fullmatch(r"[0-9]{1,20}", text):
            raise ScanError("malformed-pax")
        if key in ("mtime", "atime", "ctime"):
            try:
                if not math.isfinite(float(text)):
                    raise ValueError
            except ValueError:
                raise ScanError("malformed-pax") from None
        result[key] = text
        if len(result) > MAX_PAX_FIELDS:
            raise ScanError("resource-limit")
        offset = end
    return result


def strict_tarinfo(state):
    class StrictTarInfo(tarfile.TarInfo):
        @classmethod
        def fromtarfile(cls, archive):
            local = {}
            global_fields = getattr(archive, "_sharing_global_pax", {})
            extensions = 0
            while True:
                offset = archive.fileobj.tell()
                block = exact(archive.fileobj, 512)
                if block == bytes(512):
                    if extensions or exact(archive.fileobj, 512) != bytes(512):
                        raise ScanError("malformed-tar")
                    trailing = 0
                    while True:
                        data = archive.fileobj.read(CHUNK_BYTES)
                        if not data:
                            break
                        trailing += len(data)
                        if data.strip(b"\0"):
                            raise ScanError("malformed-tar")
                    if trailing % 512:
                        raise ScanError("malformed-tar")
                    raise tarfile.EOFHeaderError("end")
                try:
                    member = cls.frombuf(block, archive.encoding, archive.errors)
                except tarfile.HeaderError:
                    raise ScanError("malformed-tar") from None
                state.member()
                state.path(member.name)
                state.path(member.linkname)
                if not 0 <= member.size <= MAX_MEMBER_BYTES:
                    raise ScanError("resource-limit")
                member.offset = offset
                if member.type in (
                    tarfile.XHDTYPE,
                    tarfile.XGLTYPE,
                    tarfile.SOLARIS_XHDTYPE,
                    tarfile.GNUTYPE_LONGNAME,
                    tarfile.GNUTYPE_LONGLINK,
                ):
                    extensions += 1
                    if (
                        extensions > MAX_EXTENSION_HEADERS
                        or member.size > MAX_PAX_BYTES
                    ):
                        raise ScanError("resource-limit")
                    payload = exact(archive.fileobj, member.size)
                    # Check raw bounded metadata before any parsing, decoding or
                    # opaque-value handling. Compressed bytes are not an oracle
                    # for markers in expanded OCI PAX/long-name metadata.
                    if FORBIDDEN_CONTENT in payload:
                        state.violation = state.violation or "forbidden-content"
                    padding = exact(archive.fileobj, (-member.size) % 512)
                    if padding.strip(b"\0"):
                        raise ScanError("malformed-pax")
                    if member.type in (
                        tarfile.GNUTYPE_LONGNAME,
                        tarfile.GNUTYPE_LONGLINK,
                    ):
                        if not payload.endswith(b"\0") or b"\0" in payload[:-1]:
                            raise ScanError("malformed-tar")
                        key = (
                            "path"
                            if member.type == tarfile.GNUTYPE_LONGNAME
                            else "linkpath"
                        )
                        local[key] = payload[:-1].decode("utf-8", "surrogateescape")
                    else:
                        fields = pax_fields(payload)
                        if member.type == tarfile.XGLTYPE:
                            global_fields = {**global_fields, **fields}
                            archive._sharing_global_pax = global_fields
                        else:
                            local.update(fields)
                    if len(global_fields) + len(local) > MAX_PAX_FIELDS:
                        raise ScanError("resource-limit")
                    # Check overridden paths too, not only the final effective name.
                    for fields in (local, global_fields):
                        for key in ("path", "linkpath"):
                            if key in fields:
                                state.path(fields[key])
                    continue
                fields = {**global_fields, **local}
                member._apply_pax_info(fields, archive.encoding, archive.errors)
                state.path(member.name)
                state.path(member.linkname)
                if not 0 <= member.size <= MAX_MEMBER_BYTES:
                    raise ScanError("resource-limit")
                if member.type not in (
                    tarfile.REGTYPE,
                    tarfile.AREGTYPE,
                    tarfile.DIRTYPE,
                    tarfile.SYMTYPE,
                    tarfile.LNKTYPE,
                    tarfile.CHRTYPE,
                    tarfile.BLKTYPE,
                    tarfile.FIFOTYPE,
                ):
                    raise ScanError("unsupported-tar")
                if not member.isreg() and member.size:
                    raise ScanError("malformed-tar")
                # Builtin processing computes forward-only data offsets. PAX has
                # already been strictly applied; archive.pax_headers stays empty.
                return member._proc_builtin(archive)

    return StrictTarInfo


def scan_tar(source, state, visit=None):
    with tarfile.open(
        fileobj=source, mode="r|", tarinfo=strict_tarinfo(state)
    ) as archive:
        for member in archive:
            # Python <3.13 caches members even in r| mode. No member lookup is
            # needed here, including for links, so keep that memory bounded.
            archive.members.clear()
            if visit is not None:
                visit(archive, member)
            elif member.isreg():
                drain(Reader(MemberReader(archive, member), state, inspect=True))


def read_json(source):
    payload = bytearray()
    while True:
        data = source.read(min(CHUNK_BYTES, MAX_JSON_BYTES + 1 - len(payload)))
        if not data:
            break
        payload.extend(data)
        if len(payload) > MAX_JSON_BYTES:
            raise ScanError("resource-limit")

    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError
            result[key] = value
        return result

    def reject_constant(value):
        raise ValueError

    try:
        result = json.loads(
            payload.decode("utf-8"),
            object_pairs_hook=pairs,
            parse_constant=reject_constant,
        )
    except (ValueError, UnicodeError, RecursionError):
        raise ScanError("malformed-json") from None
    pending = [(result, 0)]
    while pending:
        value, depth = pending.pop()
        if depth > MAX_JSON_DEPTH:
            raise ScanError("resource-limit")
        if isinstance(value, dict):
            pending.extend((child, depth + 1) for child in value.values())
        elif isinstance(value, list):
            pending.extend((child, depth + 1) for child in value)
        elif isinstance(value, float) and not math.isfinite(value):
            raise ScanError("malformed-json")
    return result


def descriptor(value, state, role):
    if not isinstance(value, dict):
        raise ScanError("malformed-oci")
    digest, size, media = value.get("digest"), value.get("size"), value.get("mediaType")
    match = DIGEST.fullmatch(digest) if isinstance(digest, str) else None
    if (
        not match
        or type(size) is not int
        or not 0 <= size <= MAX_MEMBER_BYTES
        or not isinstance(media, str)
        or len(media) > MAX_NAME_BYTES
    ):
        raise ScanError("malformed-oci")
    compression = None
    if role == "index":
        kind = (
            "index"
            if media in INDEX_MEDIA
            else "manifest"
            if media in MANIFEST_MEDIA
            else None
        )
        if kind is None:
            raise ScanError("unsupported-oci")
    elif role == "layer":
        kind, compression = "layer", LAYER_MEDIA.get(media)
        if compression is None:
            raise ScanError("unsupported-compression")
    else:
        kind = "json"
    state.references.append((match[1], size, kind, compression))
    if len(state.references) > MAX_DESCRIPTORS:
        raise ScanError("resource-limit")


def json_kind(value, state):
    if isinstance(value, dict) and value.get("schemaVersion") == 2:
        if "manifests" in value:
            if not isinstance(value["manifests"], list) or not value["manifests"]:
                raise ScanError("malformed-oci")
            for item in value["manifests"]:
                descriptor(item, state, "index")
            return "index"
        if "config" in value or "layers" in value:
            if not isinstance(value.get("layers"), list):
                raise ScanError("malformed-oci")
            descriptor(value.get("config"), state, "config")
            for item in value["layers"]:
                descriptor(item, state, "layer")
            return "manifest"
    return "json"


def scan_blob(source, state):
    prefix = exact_prefix(source, 512)
    replay = PrefixReader(prefix, source)
    compression = (
        "gzip"
        if prefix.startswith(b"\x1f\x8b")
        else "bz2"
        if prefix.startswith(b"BZh")
        else "xz"
        if prefix.startswith(b"\xfd7zXZ\0")
        else None
    )
    if prefix.startswith(b"\x28\xb5\x2f\xfd"):
        raise ScanError("unsupported-compression")
    if compression:
        expanded = Reader(DecompressReader(replay, compression), state, account=True)
        scan_tar(expanded, state)
        return "layer", compression
    if len(prefix) == 512:
        try:
            tarfile.TarInfo.frombuf(prefix, "utf-8", "surrogateescape")
            is_tar = True
        except tarfile.HeaderError:
            is_tar = prefix == bytes(512)
        if is_tar:
            scan_tar(replay, state)
            return "layer", "tar"
    if not prefix.lstrip() or prefix.lstrip()[:1] in b'{["-0123456789tfn':
        return json_kind(read_json(replay), state), None
    raise ScanError("unsupported-blob")


def exact_prefix(source, size):
    result = bytearray()
    while len(result) < size:
        data = source.read(size - len(result))
        if not data:
            break
        result.extend(data)
    return bytes(result)


def scan_oci(source, state):
    seen = set()

    def visit(archive, member):
        name = member.name
        while name.startswith("./"):
            name = name[2:]
        if member.isdir():
            if name.rstrip("/") not in ("", ".", "blobs", "blobs/sha256"):
                raise ScanError("unexpected-oci-entry")
            return
        if not member.isreg():
            raise ScanError("unexpected-oci-entry")
        if name in seen:
            raise ScanError("malformed-oci")
        seen.add(name)
        match = BLOB_PATH.fullmatch(name)
        if name not in ("oci-layout", "index.json") and not match:
            raise ScanError("unexpected-oci-entry")
        reader = Reader(
            MemberReader(archive, member), state, inspect=True, digest=bool(match)
        )
        if match:
            if len(state.blobs) >= MAX_BLOBS:
                raise ScanError("resource-limit")
            kind, compression = scan_blob(reader, state)
            # Classification always consumes the complete member, including
            # compressed trailers; digest verification is therefore exact.
            if reader.hash.hexdigest() != match[1]:
                raise ScanError("malformed-oci")
            state.blobs[match[1]] = (member.size, kind, compression)
        else:
            value = read_json(reader)
            if name == "oci-layout":
                if (
                    not isinstance(value, dict)
                    or value.get("imageLayoutVersion") != "1.0.0"
                ):
                    raise ScanError("malformed-oci")
            elif json_kind(value, state) != "index":
                raise ScanError("malformed-oci")

    scan_tar(source, state, visit)
    if not {"oci-layout", "index.json"} <= seen:
        raise ScanError("malformed-oci")
    for digest, size, kind, compression in state.references:
        actual = state.blobs.get(digest)
        if actual is None or actual[0] != size:
            raise ScanError("malformed-oci")
        if kind == "json":
            if actual[1] not in ("json", "manifest", "index"):
                raise ScanError("malformed-oci")
        elif actual[1:] != (kind, compression):
            raise ScanError("malformed-oci")


def scan_archive(source, mode):
    """Consume a nonseekable source to EOF; raise only fixed ScanError values.

    Even on a parse refusal, drain remaining raw input so the producer can
    finish authentication. The total-byte limit takes priority over draining
    an unbounded adversarial stream. No source is closed by this function.
    """
    state = State()
    reader = Reader(source, state, account=True)
    error = None
    try:
        if mode == "tar":
            scan_tar(reader, state)
        elif mode == "oci":
            scan_oci(reader, state)
        else:
            raise ScanError("invalid-mode")
    except ScanError as exc:
        error = exc
    except tarfile.TarError:
        error = ScanError("malformed-tar")
    except Exception:
        # Never pass parser/decompressor messages (which can include filenames
        # or input content) to a caller or the command-line diagnostic.
        error = ScanError("malformed-input")
    try:
        if state.bytes <= MAX_TOTAL_BYTES:
            drain(reader)
    except Exception:
        error = ScanError(
            "resource-limit" if state.bytes > MAX_TOTAL_BYTES else "input-error"
        )
    if error is not None:
        raise error from None
    if state.violation:
        raise ScanError(state.violation)


def main(argv=None, *, stdin=None, stdout=None, stderr=None):
    argv = sys.argv[1:] if argv is None else argv
    stdin = sys.stdin.buffer if stdin is None else stdin
    stdout = sys.stdout if stdout is None else stdout
    stderr = sys.stderr if stderr is None else stderr
    try:
        if len(argv) != 1 or argv[0] not in ("tar", "oci"):
            raise ScanError("invalid-mode")
        scan_archive(stdin, argv[0])
    except KeyboardInterrupt:
        stdout.write(REFUSAL)
        stderr.write("archive-scan:interrupted\n")
        return 1
    except ScanError as exc:
        stdout.write(REFUSAL)
        stderr.write("archive-scan:" + str(exc) + "\n")
        return 1
    stdout.write(SUCCESS)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
