#!/usr/bin/env python3
"""Bind bounded, nonexecuted package archives to a saved clean release identity.

Run after all source tests, builds and installed-package checks. Publication
also checks existing package assets from a fresh canonical GitHub release API
response. No assets is valid for an initial release: the external GitHub
publisher must subsequently attach these exact workflow outputs.

Missing GitHub digests require a bounded anonymous canonical asset download;
unknown digest algorithms fail closed. No URL from the API payload is used.
"""

import argparse
import gzip
import hashlib
import json
import os
import re
import stat
import struct
import subprocess
import tarfile
import zipfile
import zlib
from email.parser import BytesParser
from http.client import HTTPException
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

if __package__:
    from . import check_release
else:
    import check_release


MAX_ARCHIVE = 64 * 1024 * 1024
MAX_EXPANDED = 128 * 1024 * 1024
MAX_METADATA = 256 * 1024
MAX_MEMBERS = 8192
MAX_RELEASE_JSON = 4 * 1024 * 1024
CHUNK = 64 * 1024
IDENTITY_NAME = "validated-identity.json"
CHECKSUM_NAME = "SHA256SUMS"


def refuse(message):
    raise ValueError(message)


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            refuse("Duplicate JSON metadata key")
        result[key] = value
    return result


def load_json(data):
    def invalid_constant(_value):
        refuse("Invalid JSON metadata constant")

    return json.loads(
        data, object_pairs_hook=unique_object, parse_constant=invalid_constant
    )


def bounded_file(path, limit):
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
        refuse("Expected a bounded regular input file")
    with path.open("rb") as stream:
        data = stream.read(limit + 1)
    if len(data) > limit:
        refuse("Input exceeds its size bound")
    return data


def file_digest(path):
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or not 0 < info.st_size <= MAX_ARCHIVE:
        refuse("Expected a bounded regular package archive")
    total = 0
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(CHUNK):
            total += len(chunk)
            if total > MAX_ARCHIVE:
                refuse("Archive exceeds its size bound")
            digest.update(chunk)
    if total != info.st_size:
        refuse("Archive changed during validation")
    return digest.hexdigest(), total


def safe_member(name, *, directory=False):
    if directory and name.endswith("/"):
        name = name[:-1]
    if (
        not name
        or "\\" in name
        or "\x00" in name
        or any(part in ("", ".", "..") for part in name.split("/"))
        or len(name.encode("utf-8")) > 1024
    ):
        refuse("Unsafe archive member path")
    return name


def metadata_bytes(stream, size):
    if size < 0 or size > MAX_METADATA:
        refuse("Archive metadata exceeds its size bound")
    data = stream.read(MAX_METADATA + 1)
    if len(data) != size:
        refuse("Invalid archive metadata size")
    return data


def wheel_metadata(path, wanted):
    # ZipFile eagerly loads the central directory. Bound its advertised size
    # and member count before constructing it; this project needs no ZIP64.
    # Include the locator even with the longest legal ZIP comment.
    trailer_size = 65535 + 22 + 20
    with path.open("rb") as stream:
        stream.seek(max(0, path.stat().st_size - trailer_size))
        tail = stream.read(trailer_size)
    offset = tail.rfind(b"PK\x05\x06")
    if offset < 0 or len(tail) - offset < 22:
        refuse("Invalid wheel directory")
    header = struct.unpack("<4s4H2LH", tail[offset : offset + 22])
    _, disk, central_disk, on_disk, count, size, start, comment = header
    if (
        disk != 0
        or central_disk != 0
        or on_disk != count
        or count == 65535
        or count > MAX_MEMBERS
        or size > 4 * 1024 * 1024
        or start == 0xFFFFFFFF
        or start + size > path.stat().st_size
        or offset + 22 + comment != len(tail)
        or (offset >= 20 and tail[offset - 20 : offset - 16] == b"PK\x06\x07")
    ):
        refuse("Wheel directory exceeds its bounds")
    result = {}
    seen = set()
    total = 0
    with zipfile.ZipFile(path) as archive:
        members = archive.infolist()
        if len(members) != count:
            refuse("Invalid wheel member count")
        for member in members:
            name = safe_member(member.filename, directory=member.is_dir())
            mode = member.external_attr >> 16
            if (
                name in seen
                or (stat.S_IFMT(mode) not in (0, stat.S_IFREG, stat.S_IFDIR))
                or (stat.S_ISDIR(mode) and not member.is_dir())
            ):
                refuse("Duplicate or special wheel member")
            seen.add(name)
            total += member.file_size
            if total > MAX_EXPANDED:
                refuse("Wheel contents exceed their size bound")
            if name in wanted:
                if member.is_dir():
                    refuse("Metadata must be a regular file")
                with archive.open(member) as stream:
                    result[name] = metadata_bytes(stream, member.file_size)
    if set(result) != set(wanted):
        refuse("Required wheel metadata is missing")
    return result


class BoundedReader:
    """Bound even tar headers/PAX data and skipped compressed member bodies."""

    def __init__(self, stream):
        self.stream = stream
        self.total = 0

    def read(self, size=-1):
        remaining = MAX_EXPANDED - self.total
        data = self.stream.read(remaining + 1 if size < 0 else min(size, remaining + 1))
        self.total += len(data)
        if self.total > MAX_EXPANDED:
            refuse("Source archive contents exceed their size bound")
        return data


def sdist_metadata(path, root, wanted):
    result = {}
    seen = set()
    total = 0
    with (
        gzip.open(path, "rb") as compressed,
        tarfile.open(fileobj=BoundedReader(compressed), mode="r|") as archive,
    ):
        for index, member in enumerate(archive):
            if index >= MAX_MEMBERS:
                refuse("Too many source archive members")
            name = safe_member(member.name, directory=member.isdir())
            if name in seen or not (member.isfile() or member.isdir()):
                refuse("Duplicate or special source archive member")
            if member.sparse is not None:
                refuse("Sparse source archive member")
            if member.size < 0:
                refuse("Negative source archive member size")
            if name != root and not name.startswith(root + "/"):
                refuse("Unexpected source archive root")
            if name == root and not member.isdir():
                refuse("Source archive root must be a directory")
            seen.add(name)
            total += member.size
            if total > MAX_EXPANDED:
                refuse("Source archive contents exceed their size bound")
            if name in wanted:
                if not member.isfile():
                    refuse("Metadata must be a regular file")
                stream = archive.extractfile(member)
                if stream is None:
                    refuse("Source metadata is unreadable")
                with stream:
                    result[name] = metadata_bytes(stream, member.size)
    if set(result) != set(wanted):
        refuse("Required source archive metadata is missing")
    return result


def check_package_metadata(data, identity):
    metadata = BytesParser().parsebytes(data)
    if metadata.get_all("Name") != ["oc-box"] or metadata.get_all("Version") != [
        identity["distribution_version"]
    ]:
        refuse("Package metadata differs from the validated identity")


def check_embedded(build_data, source, identity, *, wheel):
    build = load_json(build_data)
    if not isinstance(build, dict):
        refuse("Invalid embedded build identity")
    for key in ("commit", "commit_count", "version", "release_channel", "dirty"):
        if (
            type(build.get(key)) is not type(identity[key])
            or build[key] != identity[key]
        ):
            refuse("Embedded build " + key + " differs from the validated identity")
    if (wheel or "distribution_version" in build) and (
        build.get("distribution_version") != identity["distribution_version"]
    ):
        refuse("Embedded public version differs from the validated identity")
    if build["dirty"] is not False:
        refuse("Dirty embedded build identity")
    if hashlib.sha256(source).hexdigest() != identity["source_sha256"]:
        refuse("Embedded version source differs from the validated identity")
    # Parse, but never import/execute source from either archive.
    _, channel, _ = check_release.source_identity(source.decode("utf-8"))
    if channel != identity["release_channel"]:
        refuse("Embedded source channel differs from the validated identity")


def check_archives(directory, identity):
    version = identity["distribution_version"]
    wheel_name = f"oc_box-{version}-py3-none-any.whl"
    sdist_name = f"oc_box-{version}.tar.gz"
    paths = sorted([*directory.glob("*.whl"), *directory.glob("*.tar.gz")])
    if {path.name for path in paths} != {wheel_name, sdist_name} or len(paths) != 2:
        refuse("Expected exactly the validated oc_box wheel and source filenames")
    digests = {path.name: file_digest(path) for path in paths}
    wheel_source = "oc_box/version_info.py"
    wheel_build = "oc_box/build-info.json"
    wheel_distribution = "oc_box/distribution.json"
    wheel_info = f"oc_box-{version}.dist-info/METADATA"
    wheel = wheel_metadata(
        directory / wheel_name,
        (wheel_source, wheel_build, wheel_distribution, wheel_info),
    )
    check_package_metadata(wheel[wheel_info], identity)
    check_embedded(wheel[wheel_build], wheel[wheel_source], identity, wheel=True)
    if load_json(wheel[wheel_distribution]) != {
        "installer": "python-package",
        "name": "oc-box",
        "version": version,
    }:
        refuse("Wheel distribution ownership metadata differs")
    root = f"oc_box-{version}"
    source_path = root + "/src/oc_box/version_info.py"
    build_path = root + "/build-info.json"
    info_path = root + "/PKG-INFO"
    source = sdist_metadata(
        directory / sdist_name, root, (source_path, build_path, info_path)
    )
    check_package_metadata(source[info_path], identity)
    check_embedded(source[build_path], source[source_path], identity, wheel=False)
    return digests


def allowed_download_url(url):
    parsed = urlsplit(url)
    return (
        parsed.scheme == "https"
        and parsed.hostname
        in {
            "github.com",
            "release-assets.githubusercontent.com",
            "objects.githubusercontent.com",
        }
        and parsed.port is None
        and parsed.username is None
        and parsed.password is None
        and not parsed.fragment
    )


class CanonicalRedirect(HTTPRedirectHandler):
    max_redirections = 5
    max_repeats = 2

    def redirect_request(self, request, response, code, message, headers, new_url):
        if not allowed_download_url(new_url):
            refuse("GitHub asset redirect requires independent verification")
        return super().redirect_request(
            request, response, code, message, headers, new_url
        )


def download_digest(tag, name, size):
    # Both components are derived from the validated identity, never API URLs.
    if (
        not re.fullmatch(r"v\d+\.\d+\.\d+(?:\.dev0)?", tag)
        or name
        not in {
            f"oc_box-{tag[1:]}-py3-none-any.whl",
            f"oc_box-{tag[1:]}.tar.gz",
        }
        or type(size) is not int
        or not 0 < size <= MAX_ARCHIVE
    ):
        refuse("Invalid canonical GitHub asset selector")
    url = f"https://github.com/groopcd/oc-box/releases/download/{tag}/{name}"
    request = Request(url, headers={"User-Agent": "oc-box-release-artifact-check"})
    # Disable ambient proxy discovery, including proxy credentials. A fresh
    # opener has no authentication handler and receives no tokens.
    with build_opener(ProxyHandler({}), CanonicalRedirect()).open(
        request, timeout=20
    ) as response:
        if not allowed_download_url(response.geturl()):
            refuse("GitHub asset origin requires independent verification")
        if response.status != 200:
            refuse("GitHub asset requires independent verification")
        length = response.headers.get("Content-Length")
        if length is not None and (not length.isdecimal() or int(length) != size):
            refuse("GitHub asset size differs from the built archive")
        total = 0
        digest = hashlib.sha256()
        while chunk := response.read(min(CHUNK, size + 1 - total)):
            total += len(chunk)
            if total > size or total > MAX_ARCHIVE:
                refuse("GitHub asset download exceeds its size bound")
            digest.update(chunk)
        if total != size:
            refuse("GitHub asset size differs from the built archive")
    return digest.hexdigest()


def check_published_assets(release, identity, digests, *, downloader=download_digest):
    assets = release.get("assets")
    if not isinstance(assets, list) or len(assets) > MAX_MEMBERS:
        refuse("A complete bounded GitHub release asset inventory is required")
    seen = set()
    for asset in assets:
        if not isinstance(asset, dict) or not isinstance(asset.get("name"), str):
            refuse("Invalid GitHub release asset metadata")
        name = asset["name"]
        if not name.endswith((".whl", ".tar.gz")):
            continue
        if name not in digests or name in seen:
            refuse("Unexpected or duplicate GitHub package asset name")
        seen.add(name)
        expected, size = digests[name]
        if type(asset.get("size")) is not int or asset["size"] != size:
            refuse("GitHub package asset size differs from the built archive")
        if asset.get("state") != "uploaded":
            refuse("GitHub package asset is not completely uploaded")
        digest = asset.get("digest")
        if digest is None or digest == "":
            actual = downloader(identity["tag"], name, size)
        elif isinstance(digest, str) and re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
            actual = digest.removeprefix("sha256:")
        else:
            refuse("Unknown GitHub asset digest requires independent verification")
        if actual != expected:
            refuse("GitHub package asset checksum differs from the built archive")


def check(root, directory, identity, *, release=None, downloader=download_digest):
    if (
        not isinstance(identity, dict)
        or type(identity.get("publish_validated")) is not bool
    ):
        refuse("Invalid saved release identity")
    publishing = identity["publish_validated"]
    if not publishing and release is not None:
        refuse("Build-only validation cannot consume a release inventory")

    def revalidate():
        current = check_release.validate(
            root,
            publish=publishing,
            tag=identity.get("tag") if publishing else None,
            release=release,
        )
        if current != identity:
            refuse("Source identity changed after initial validation")

    revalidate()
    digests = check_archives(directory, identity)
    if publishing:
        check_published_assets(release, identity, digests, downloader=downloader)
    # Downloads and archive inspection may take time. Refuse late source or
    # artifact mutations before writing the publishable bundle.
    revalidate()
    if any(file_digest(directory / name) != value for name, value in digests.items()):
        refuse("Package archives changed during final validation")
    return digests


def write_evidence(path, data):
    # Never follow a preexisting symlink or truncate a hardlinked source file.
    descriptor = os.open(
        path, os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o644
    )
    with os.fdopen(descriptor, "wb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            refuse("Release evidence must be a single-link regular output file")
        stream.truncate(0)
        stream.write(data)


def write_bundle(directory, identity, digests):
    data = (json.dumps(identity, sort_keys=True, indent=2) + "\n").encode()
    write_evidence(directory / IDENTITY_NAME, data)
    checksums = {name: value[0] for name, value in digests.items()}
    checksums[IDENTITY_NAME] = hashlib.sha256(data).hexdigest()
    write_evidence(
        directory / CHECKSUM_NAME,
        "".join(
            f"{digest}  {name}\n" for name, digest in sorted(checksums.items())
        ).encode(),
    )


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("distribution_dir", type=Path)
    parser.add_argument("--root", type=Path, default=Path("."))
    parser.add_argument("--identity", type=Path, required=True)
    parser.add_argument("--release-json", type=Path)
    args = parser.parse_args(argv)
    try:
        identity = load_json(bounded_file(args.identity, MAX_METADATA))
        release = (
            load_json(bounded_file(args.release_json, MAX_RELEASE_JSON))
            if args.release_json is not None
            else None
        )
        digests = check(args.root, args.distribution_dir, identity, release=release)
        write_bundle(args.distribution_dir, identity, digests)
    except (
        OSError,
        ValueError,
        SyntaxError,
        EOFError,
        RuntimeError,
        HTTPException,
        subprocess.SubprocessError,
        tarfile.TarError,
        zipfile.BadZipFile,
        zlib.error,
    ):
        # Never echo release bodies, private paths, URLs or server diagnostics.
        parser.exit(
            1,
            "Release artifact validation refused; verify source, metadata and existing asset checksums.\n",
        )
    print(
        "Validated clean source, exact wheel/sdist identity and existing release assets."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
