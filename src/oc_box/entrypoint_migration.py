"""Replace a stopped legacy container without replacing or initializing its home."""

import hashlib
import json
import os
import re
import secrets
import stat
import subprocess
import sys

if __package__:
    from . import (
        history_recovery,
        startup_output,
        workspace_control,
        workspace_identity,
    )
else:
    import history_recovery
    import startup_output
    import workspace_control
    import workspace_identity


FIELD = "entrypoint_conversion"
RECEIPT = "converted_entrypoint"
RETIRE = "entrypoint_retirement"
_LABEL = "io.oc-box.entrypoint-conversion"
_MANAGED = "io.oc-box.managed"
_HEX = re.compile(r"[0-9a-f]{64}")
_NONCE = re.compile(r"[0-9a-f]{32}")
_OPTIONS = (
    "NetworkMode",
    "UsernsMode",
    "IDMappings",
    "PidMode",
    "IpcMode",
    "UTSMode",
    "CgroupMode",
    "CgroupParent",
    "CgroupnsMode",
    "CpuPeriod",
    "CpuQuota",
    "CpuShares",
    "CpusetCpus",
    "CpusetMems",
    "Memory",
    "MemorySwap",
    "PidsLimit",
    "SecurityOpt",
    "CapAdd",
    "CapDrop",
    "GroupAdd",
    "ReadonlyRootfs",
    "Runtime",
    "Init",
    "Dns",
    "DnsOptions",
    "DnsSearch",
    "ExtraHosts",
    "ShmSize",
    "OomKillDisable",
)
PENDING = (
    "Legacy container replacement is not complete. No new startup was admitted. "
    "Retry 'oc-box start --convert-legacy-container'; if verification still fails, "
    "keep both resources and get help. Do not reset or purge."
)
RETIRE_PENDING = (
    "Legacy container replacement is waiting for verified startup or retirement. "
    "Do not start the old container, reset, purge, or downgrade."
)
REFUSED = (
    "Cannot verify a standard stopped legacy container and its original private home. "
    "No new startup was admitted. Keep the existing data; do not reset or purge."
)
OFFER_REBUILD = (
    "The stopped container's OS snapshot could not be made. The original container, "
    "project and private conversations are retained. To discard only changes to the "
    "container OS and rebuild tools while keeping the same project and private home, "
    "run 'oc-box start --rebuild-legacy-container'. This is not 'oc-box purge'."
)


class Unverified(ValueError):
    pass


def _require(condition):
    if not condition:
        raise Unverified


def _hash(value):
    return type(value) is str and _HEX.fullmatch(value) is not None


def _image(value):
    ident = value.removeprefix("sha256:") if type(value) is str else None
    _require(_hash(ident))
    return ident


def _stopped(info, statuses):
    state = info.get("State", {})
    return (
        state.get("Status") in statuses
        and all(state.get(key) is False for key in ("Running", "Paused", "Restarting"))
        and not state.get("Dead", False)
        and type(state.get("Pid")) is int
        and state["Pid"] == 0
        and state.get("ConmonPid", 0) == 0
    )


def _stamp(value):
    return (
        type(value) is list
        and len(value) == 2
        and all(type(part) is int and part >= 0 for part in value)
    )


def volume_stamp(path):
    metadata = os.lstat(path)
    _require(stat.S_ISDIR(metadata.st_mode))
    return [metadata.st_dev, metadata.st_ino]


def initialized_volume(volume):
    # Podman may leave both flags set when the original image has no files at
    # the home path. With copy-up still outstanding, no copy-up chown is in
    # progress. For a populated local volume Podman then clears NeedsChown
    # without changing its owner or mode. The replacement MUST use :nocopy,
    # independently checked from its stored create arguments.
    copy = volume.get("NeedsCopyUp", False)
    chown = volume.get("NeedsChown", False)
    if type(copy) is not bool or type(chown) is not bool:
        return False
    if not chown:
        return True
    if not copy:
        return False
    fd = os.open(volume["Mountpoint"], os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        with os.scandir(fd) as entries:
            return next(entries, None) is not None
    finally:
        os.close(fd)


def _create_argv(info):
    argv = info["Config"].get("CreateCommand")
    if (
        type(argv) is list
        and 0 < len(argv) <= 256
        and all(type(value) is str for value in argv)
    ):
        return argv
    return None


def no_copy_binding(data, info):
    argv = _create_argv(info)
    binding = f"{data['home_volume']}:{data['home']}:rw,nocopy"
    return (
        argv is not None
        and sum(
            1
            for option, value in zip(argv, argv[1:])
            if option == "--volume" and value == binding
        )
        == 1
    )


def _standard_process_limits(info):
    argv = _create_argv(info)
    if argv is None or any(
        option == "--ulimit" or option.startswith("--ulimit=") for option in argv
    ):
        return False
    # Inspect does not establish whether these values were inherited. Keep the
    # prior refusal of nonstandard rows, but never compare or replay the values.
    limits = info["HostConfig"].get("Ulimits")
    if limits is None:
        return True
    if type(limits) is not list or len(limits) > 2:
        return False
    seen = set()
    for row in limits:
        if (
            type(row) is not dict
            or set(row) != {"Name", "Soft", "Hard"}
            or type(row["Name"]) is not str
        ):
            return False
        name = row["Name"].lower().removeprefix("rlimit_")
        soft, hard = row["Soft"], row["Hard"]
        if (
            name not in ("nofile", "nproc")
            or name in seen
            or not all(
                type(value) is int and 0 <= value < 2**63 for value in (soft, hard)
            )
            or soft > hard
        ):
            return False
        seen.add(name)
    return True


def replacement_matches(data, info):
    record = data.get(RECEIPT)
    return (
        type(record) is dict
        and set(record) == {"id", "bootstrap", "volume"}
        and _hash(record["id"])
        and type(record["bootstrap"]) is bool
        and _stamp(record["volume"])
        and record["id"] == info.get("Id")
    )


def fresh_replacement(data, info):
    """Only a verified conversion can extend guarded admission to a fresh object."""
    return replacement_matches(data, info) and _stopped(info, ("created", "configured"))


def needs_bootstrap(data):
    record = data.get(RECEIPT)
    return type(record) is dict and record.get("bootstrap") is True


def in_progress(data):
    return FIELD in data or RETIRE in data


def durable_registry(data):
    return isinstance(data, dict) and (in_progress(data) or RECEIPT in data)


def engine_environment(box):
    """Keep local Podman selectors without forwarding a launcher credential environment."""
    if any(os.environ.get(key) for key in ("CONTAINER_HOST", "CONTAINER_CONNECTION")):
        raise box.BoxError(
            "Legacy replacement requires a local Podman engine selection."
        )
    env = box.maintenance_host_env()
    for key in (
        "CONTAINERS_CONF",
        "CONTAINERS_CONF_OVERRIDE",
        "CONTAINERS_CONF_MODULES",
        "CONTAINERS_STORAGE_CONF",
        "STORAGE_DRIVER",
        "STORAGE_OPTS",
    ):
        if key in os.environ:
            env[key] = os.environ[key]
    return env


def require_local_engine(box, env):
    """Prove the forced-local selection is a rootless engine before staged work."""
    try:
        info = json.loads(
            box.podman(
                "--remote=false",
                "info",
                "--format=json",
                capture=True,
                private=True,
                timeout=15,
                env=env,
            ).stdout
        )
        _require(
            info["host"]["serviceIsRemote"] is False
            and info["host"]["security"]["rootless"] is True
        )
    except (OSError, ValueError, KeyError, TypeError, AttributeError, box.BoxError):
        raise box.BoxError(
            "Cannot verify a local rootless Podman engine for the staged replacement."
        ) from None


def running_replacement(info):
    if not isinstance(info, dict) or type(info.get("State")) is not dict:
        return False
    live = info["State"]
    return (
        live.get("Status") == "running"
        and live.get("Running") is True
        and live.get("Paused") is False
        and live.get("Restarting") is False
        and type(live.get("Pid")) is int
        and live["Pid"] > 0
    )


def consent(box, args, data):
    rebuild = getattr(args, "rebuild_legacy_container", False)
    if FIELD in data and not (
        rebuild or getattr(args, "convert_legacy_container", False)
    ):
        raise box.BoxError(PENDING)
    if rebuild or getattr(args, "convert_legacy_container", False):
        return
    if sys.stdin.isatty() and sys.stderr.isatty():
        try:
            print(
                "This older container needs a one-time replacement before guarded startup. "
                "Keep the same project, private conversations and installed OS? [y/N] ",
                file=sys.stderr,
                end="",
                flush=True,
            )
            if input().strip().lower() in ("y", "yes"):
                return
        except EOFError:
            pass
        raise box.BoxError(
            "Legacy container replacement declined. No container was changed."
        )
    raise box.BoxError(
        "Guarded startup refused this older container's PATH-dependent entrypoint. "
        "To replace it without moving the project or private conversations, "
        "run 'oc-box start --convert-legacy-container'. Do not reset or downgrade."
    )


class Conversion:
    """One local, locked swap; only a validated durable intent can be resumed."""

    def __init__(self, box, path, data, stop_token, *, env=None):
        self.box, self.path, self.data, self.stop_token = box, path, data, stop_token
        self.env = env
        self.name = data["container"]
        self.home = data["home_volume"]

    def _run(self, *args, check=True, timeout=15):
        return self.box.podman(
            "--remote=false",
            *args,
            capture=True,
            private=True,
            check=check,
            timeout=timeout,
            stdin=subprocess.DEVNULL,
            **({"env": self.env} if self.env is not None else {}),
        )

    def _object(self, kind, selector, *, missing=False):
        result = self._run(kind, "inspect", selector, check=False)
        if result.returncode:
            if (
                missing
                and self._run(kind, "exists", selector, check=False).returncode == 1
            ):
                return None
            raise Unverified
        rows = json.loads(result.stdout)
        _require(type(rows) is list and len(rows) == 1 and type(rows[0]) is dict)
        return rows[0]

    def _project(self):
        project = self.box.validate_project(self.data["project"])
        selected, current = self.box.find_workspace(project)
        _require(
            selected == self.path
            and current == self.data
            and workspace_identity.same_project(self.data, project)
        )
        return project

    def _volume(self, mountpoint=None):
        volume = self._object("volume", self.home)
        source = volume["Mountpoint"]
        _require(
            type(source) is str
            and os.path.isabs(source)
            and (mountpoint is None or mountpoint == source)
            and volume["Driver"] == "local"
            and volume["Name"] == self.home
            and not volume.get("Options")
            and volume["Labels"].get(self.box.LABEL) == self.data["id"]
            and initialized_volume(volume)
        )
        return volume_stamp(source)

    def _users(self, *expected):
        result = self._run(
            "ps",
            "--all",
            "--no-trunc",
            "--filter",
            f"volume={self.home}",
            "--format",
            "{{.ID}}",
        )
        _require(sorted(result.stdout.split()) == sorted(expected))

    def _container(self, selector, *, legacy, missing=False):
        item = self._object("container", selector, missing=missing)
        if item is None:
            return None
        state = ("exited", "stopped") if legacy else ("created", "configured")
        _require(_hash(item["Id"]) and _stopped(item, state))
        config, host = item["Config"], item["HostConfig"]
        _require(
            config["Labels"].get(_MANAGED) == "true"
            and config["User"] == "0:0"
            and config["WorkingDir"] == self.box.container_project(self.data)
            and not config.get("StartupHealthCheck")
            and host.get("Privileged") is False
            and host.get("UsernsMode") in (None, "", "host", "private")
            and not any(
                host.get(key)
                for key in (
                    "AutoRemove",
                    "Devices",
                    "DeviceRequests",
                    "Tmpfs",
                    "PortBindings",
                    "PublishAllPorts",
                    "VolumesFrom",
                    "Links",
                )
            )
            and not item.get("Dependencies")
            and not item.get("IsInfra")
            and not item.get("IsService")
            and not item.get("Rootfs")
            and len(item["Mounts"]) == 2
            and _standard_process_limits(item)
        )
        check = (
            history_recovery.legacy_migration_mounts
            if legacy
            else history_recovery.passive_workspace_mounts
        )
        mounts = check(self.box, self.data, item)
        _require(
            (legacy or no_copy_binding(self.data, item))
            and all(
                mount.get("RW") is True
                and not mount.get("SubPath")
                and not {"U", "idmap"}.intersection(mount.get("Options") or ())
                for mount in item["Mounts"]
            )
        )
        # Refuse nonstandard create-time environment rather than copy it into
        # a new image. Credential refresh during later normal startup is separate.
        hostname = config.get("Hostname", "")
        _require(
            all(
                variable == "HOME=" + self.data["home"]
                or variable.startswith("PATH=")
                or variable in ("container=podman", "container=oci")
                or (
                    type(hostname) is str
                    and re.fullmatch(r"[a-zA-Z0-9._-]{1,253}", hostname)
                    and variable == "HOSTNAME=" + hostname
                )
                or re.fullmatch(
                    r"(?:LANG|LANGUAGE|LC_ALL|TERM)=[a-zA-Z0-9_.@:-]{1,64}", variable
                )
                for variable in config["Env"]
            )
        )
        # A previously booted container can report inherited process limits
        # that a freshly created one does not. Replaying those observations as
        # overrides can ask a rootless OCI runtime to raise its own hard limit.
        # Leave inheritance to Podman. The saved cgroup limits still match,
        # and a pending transaction from the old profile stays unadoptable.
        options = ["inherit-process-limits", *(host.get(key) for key in _OPTIONS)]
        options.append(sorted(item.get("EffectiveCaps") or ()))
        return (
            item,
            mounts,
            hashlib.sha256(
                json.dumps(options, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest(),
        )

    def _persist(self, **record):
        updated = {**self.data, FIELD: record}
        self.box.atomic_json(self.path, updated, durable=True)
        self.data.update(updated)
        return record

    def _unchanged(self):
        if not workspace_control.unchanged(self.path, self.stop_token):
            raise self.box.BoxError(
                "Stop was requested during legacy container replacement; no new startup was admitted. "
                + (
                    PENDING
                    if FIELD in self.data
                    else "The original container was not replaced."
                )
            )

    def _mutate(self, *args, timeout=15):
        with workspace_control.admission(self.path, timeout=15):
            self._unchanged()
            return self._run(*args, timeout=timeout)

    @staticmethod
    def _validated_record(record):
        _require(
            type(record) is dict
            and set(record)
            == {
                "token",
                "source",
                "base",
                "volume",
                "profile",
                "image",
                "candidate",
                "mode",
            }
            and type(record["token"]) is str
            and _NONCE.fullmatch(record["token"])
            and all(_hash(record[key]) for key in ("source", "base", "profile"))
            and _stamp(record["volume"])
            and (record["image"] is None or _hash(record["image"]))
            and (record["candidate"] is None or _hash(record["candidate"]))
            and (
                record["candidate"] is None
                or (record["image"] and record["candidate"] != record["source"])
            )
            and record["mode"] in ("snapshot", "snapshot-failed", "rebuild")
            and (record["mode"] != "snapshot-failed" or record["image"] is None)
            and (record["mode"] != "rebuild" or record["image"] == record["base"])
        )
        return record

    def _record(self):
        return self._validated_record(self.data.get(FIELD))

    def _begin(self, source_id):
        self._project()
        item, mounts, profile = self._container(self.name, legacy=True)
        _require(item["Id"] == source_id and item["Name"] == self.name)
        self._users(source_id)
        volume = self._volume(mounts[2])
        self._unchanged()
        return self._persist(
            token=secrets.token_hex(16),
            source=source_id,
            base=_image(item["Image"]),
            volume=volume,
            profile=profile,
            image=None,
            candidate=None,
            mode="snapshot",
        )

    def _snapshot(self, record, tag, *, rebuild):
        existing = self._object("image", tag, missing=True)
        if rebuild:
            _require(record["mode"] == "snapshot-failed" and existing is None)
            base = self._object("image", record["base"])
            _require(_image(base["Id"]) == record["base"])
            return self._persist(
                **{**record, "image": record["base"], "mode": "rebuild"}
            )
        if existing is None:
            if record["mode"] == "snapshot-failed":
                # This is a new attempt. An interruption must not leave the
                # previous attempt's confirmed failure as rebuild authority.
                self._unchanged()
                record = self._persist(**{**record, "mode": "snapshot"})
            # This can be slow; never hold stop admission behind the commit.
            self._unchanged()
            committed = self._run(
                "commit",
                "--quiet",
                "--pause=false",
                "--include-volumes=false",
                "--change",
                f"LABEL {_LABEL}={record['token']}",
                record["source"],
                tag,
                check=False,
                timeout=600,
            )
            if committed.returncode:
                self._unchanged()
                self._observe(record)
                if (
                    committed.returncode > 0
                    and self._object("image", tag, missing=True) is None
                ):
                    self._persist(**{**record, "mode": "snapshot-failed"})
                    raise self.box.BoxError(OFFER_REBUILD) from None
                raise Unverified
            existing = self._object("image", tag)
            _require(_image(committed.stdout.strip()) == _image(existing["Id"]))
        _require(existing["Config"]["Labels"].get(_LABEL) == record["token"])
        return self._persist(
            **{**record, "image": _image(existing["Id"]), "mode": "snapshot"}
        )

    def _names(self, record):
        prefix = "oc-box-entrypoint-" + record["token"]
        return prefix + "-new", prefix + "-old", "localhost/" + prefix

    def _observe(self, record):
        self._project()
        stage, retired, _ = self._names(record)
        source = self._container(record["source"], legacy=True, missing=True)
        lookup = record["candidate"] or stage
        candidate = self._container(lookup, legacy=False, missing=True)
        ids = []
        if source:
            item, mounts, profile = source
            _require(
                item["Id"] == record["source"]
                and item["Name"] in (self.name, retired)
                and _image(item["Image"]) == record["base"]
                and profile == record["profile"]
                and self._volume(mounts[2]) == record["volume"]
            )
            ids.append(item["Id"])
        if candidate:
            item, mounts, profile = candidate
            _require(
                record["image"]
                and _image(item["Image"]) == record["image"]
                and item["Name"] in (stage, self.name)
                and item["Id"] != record["source"]
                and (not record["candidate"] or item["Id"] == record["candidate"])
                and item["Config"]["Labels"].get(_LABEL) == record["token"]
                and profile == record["profile"]
                and self._volume(mounts[2]) == record["volume"]
            )
            ids.append(item["Id"])
        old_name = source[0]["Name"] if source else None
        new_name = candidate[0]["Name"] if candidate else None
        # A missing original cannot be accepted during cutover. Its removal
        # is a separate, durable operation after verified passive startup.
        _require(
            (
                old_name == self.name
                and new_name in (None, stage)
                and (new_name is not None or not record["candidate"])
            )
            or (
                old_name == retired
                and record["candidate"]
                and new_name in (stage, self.name)
            )
        )
        for name in (self.name, stage, retired):
            found = self._object("container", name, missing=True)
            expected = next(
                (
                    info[0]["Id"]
                    for info in (source, candidate)
                    if info and info[0]["Name"] == name
                ),
                None,
            )
            _require((found["Id"] if found else None) == expected)
        self._users(*ids)
        return source, candidate

    def perform(self, source_id, *, rebuild=False):
        _require(RETIRE not in self.data)
        info = json.loads(self._run("info", "--format=json").stdout)
        _require(
            info["host"]["serviceIsRemote"] is False
            and info["host"]["security"]["rootless"] is True
        )
        ident = self.data["id"]
        _require(
            type(ident) is str
            and _NONCE.fullmatch(ident)
            and self.name == "oc-box-" + ident
            and self.home == "oc-box-home-" + ident
        )
        if FIELD not in self.data and rebuild:
            raise self.box.BoxError(
                "Container OS rebuild is available only after a verified snapshot failure."
            )
        record = self._record() if FIELD in self.data else self._begin(source_id)
        source, candidate = self._observe(record)
        stage, retired, tag = self._names(record)
        self._unchanged()
        # A previous replace may have been visible even though its directory
        # fsync failed. Re-establish durability before any engine dispatch.
        record = self._persist(**record)
        if rebuild and record["mode"] != "rebuild":
            _require(record["mode"] == "snapshot-failed")
        if record["image"] is None:
            record = self._snapshot(record, tag, rebuild=rebuild)
            source, candidate = self._observe(record)
        if candidate is None:
            # The verified, stopped source still holds Podman's reference to
            # this exact volume; non-forced removal cannot detach it. This
            # works with engines predating :nocreate. Keep the original
            # through the replacement's verified passive startup as well.
            created = self._mutate(
                *self.box.container_create_args(
                    {**self.data, "container": stage, "image": record["image"]},
                    self.data["project"],
                    conversion=record["token"],
                )
            )
            source, candidate = self._observe(record)
            _require(candidate and candidate[0]["Id"] == created.stdout.strip())
        if not record["candidate"]:
            self._unchanged()
            record = self._persist(**{**record, "candidate": candidate[0]["Id"]})
        if source and source[0]["Name"] == self.name:
            self._mutate("rename", record["source"], retired)
            source, candidate = self._observe(record)
            _require(source and source[0]["Name"] == retired)
        if candidate[0]["Name"] == stage:
            self._mutate("rename", record["candidate"], self.name)
            source, candidate = self._observe(record)
            _require(candidate[0]["Name"] == self.name)
        _require(source and source[0]["Name"] == retired)
        self._unchanged()
        completed = dict(self.data)
        del completed[FIELD]
        completed[RECEIPT] = {
            "id": record["candidate"],
            "bootstrap": record["mode"] == "rebuild",
            "volume": record["volume"],
        }
        completed[RETIRE] = {"intent": record, "authorized": False}
        completed["image"] = record["image"]
        self.box.atomic_json(self.path, completed, durable=True)
        self.data.clear()
        self.data.update(completed)
        startup_output.line(
            "oc-box: Legacy replacement staged. The original stays stopped until the replacement passes its startup checks."
            + (
                " Baseline tools will be reinstalled."
                if needs_bootstrap(self.data)
                else ""
            ),
            flush=True,
        )

    @classmethod
    def _validated_retirement(cls, data):
        state = data.get(RETIRE)
        _require(
            FIELD not in data
            and type(state) is dict
            and set(state) == {"intent", "authorized"}
            and type(state["authorized"]) is bool
        )
        record = cls._validated_record(state["intent"])
        receipt = data[RECEIPT]
        candidate_id = record["candidate"]
        _require(
            _hash(candidate_id)
            and replacement_matches(data, {"Id": candidate_id})
            and receipt["volume"] == record["volume"]
            and record["image"] == data["image"]
            and receipt["bootstrap"] is (record["mode"] == "rebuild")
            and type(data.get("id")) is str
            and _NONCE.fullmatch(data["id"])
            and data["container"] == "oc-box-" + data["id"]
            and data["home_volume"] == "oc-box-home-" + data["id"]
        )
        return state

    def _verify_retirement(self, candidate_id, *, after_start=False):
        self._project()
        info = json.loads(self._run("info", "--format=json").stdout)
        _require(
            info["host"]["serviceIsRemote"] is False
            and info["host"]["security"]["rootless"] is True
        )
        state = self._validated_retirement(self.data)
        record = state["intent"]
        _require(candidate_id == record["candidate"])
        stage, parked, _ = self._names(record)
        canonical = self._object("container", self.name)
        _require(
            canonical["Id"] == candidate_id
            and canonical["Name"] == self.name
            and _image(canonical["Image"]) == record["image"]
            and canonical["Config"]["Labels"].get(_LABEL) == record["token"]
            and self._object("container", stage, missing=True) is None
        )
        mounts = history_recovery.passive_workspace_mounts(
            self.box, self.data, canonical
        )
        _require(
            len(canonical["Mounts"]) == 2
            and no_copy_binding(self.data, canonical)
            and self._volume(mounts[2]) == record["volume"]
        )
        if after_start:
            _require(running_replacement(canonical))
        else:
            _require(
                _stopped(canonical, ("created", "configured", "exited", "stopped"))
            )
        source = self._container(record["source"], legacy=True, missing=True)
        alias = self._object("container", parked, missing=True)
        if source is None:
            _require(state["authorized"] and alias is None)
            self._users(candidate_id)
        else:
            item, mounts, profile = source
            _require(
                item["Id"] == record["source"]
                and item["Name"] == parked
                and alias is not None
                and alias["Id"] == record["source"]
                and _image(item["Image"]) == record["base"]
                and profile == record["profile"]
                and self._volume(mounts[2]) == record["volume"]
            )
            self._users(record["source"], candidate_id)
        return state, source is not None

    def retire_started(self, candidate_id):
        """Caller holds both locks and has checked the boot and mount access."""
        state, present = self._verify_retirement(candidate_id, after_start=True)
        # Re-establish durability even if a previous fsync failed after rename.
        self._unchanged()
        updated = {**self.data, RETIRE: {**state, "authorized": True}}
        self.box.atomic_json(self.path, updated, durable=True)
        self.data.update(updated)
        self._unchanged()
        if present:
            # Use only the independently verified parked original's full ID.
            # Never remove the private volume, and never force-stop an object.
            self._run("container", "rm", state["intent"]["source"])
        _, present = self._verify_retirement(candidate_id, after_start=True)
        _require(not present)
        self._unchanged()
        completed = dict(self.data)
        del completed[RETIRE]
        self.box.atomic_json(self.path, completed, durable=True)
        self.data.clear()
        self.data.update(completed)
        startup_output.line(
            "oc-box: Legacy replacement boot and mount checks passed; old container retired.",
            flush=True,
        )


def verify_retirement(
    box, path, data, token, candidate_id, *, after_start=False, env=None
):
    try:
        conversion = Conversion(box, path, data, token, env=env)
        if after_start:
            conversion.retire_started(candidate_id)
        else:
            conversion._verify_retirement(candidate_id)
    except (
        OSError,
        ValueError,
        KeyError,
        TypeError,
        AttributeError,
        IndexError,
        box.BoxError,
    ):
        raise box.BoxError(RETIRE_PENDING) from None


def retirement_target(box, data):
    """Validate marker/receipt agreement without touching Podman or credentials."""
    try:
        return Conversion._validated_retirement(data)["intent"]["candidate"]
    except (ValueError, KeyError, TypeError, AttributeError):
        raise box.BoxError(RETIRE_PENDING) from None


def stop_staged(box, data, *, env=None, guarded_candidate=None):
    """Stop only a verified canonical replacement and positively confirm it is stopped."""
    candidate = retirement_target(box, data)
    options = {"env": env if env is not None else engine_environment(box)}
    stopped = ("created", "configured", "exited", "stopped")

    def inspected():
        info = box.inspect_owned(
            {**data, "container": candidate}, local_only=True, **options
        )
        _require(
            isinstance(info, dict)
            and info.get("Id") == candidate
            and info.get("Name") == data["container"]
            and replacement_matches(data, info)
        )
        return info

    try:
        with history_recovery.budget(25):
            _require(guarded_candidate is None or guarded_candidate == candidate)
            require_local_engine(box, options["env"])
            info = inspected()
            if _stopped(info, stopped):
                return
            _require(running_replacement(info))
            box.podman(
                "--remote=false",
                "stop",
                "--time",
                "10",
                candidate,
                capture=True,
                private=True,
                timeout=15,
                **options,
            )
            _require(_stopped(inspected(), stopped))
    except (
        OSError,
        ValueError,
        KeyError,
        TypeError,
        AttributeError,
        IndexError,
        box.BoxError,
    ):
        raise box.BoxError(
            "Could not verify the staged replacement stopped; no substitute was stopped."
        ) from None


def convert(box, path, data, token, source_id, *, rebuild=False, env=None):
    try:
        Conversion(box, path, data, token, env=env).perform(source_id, rebuild=rebuild)
    except box.BoxError as exc:
        if str(exc).startswith(
            ("Stop was requested", OFFER_REBUILD, "Container OS rebuild")
        ):
            raise
        raise box.BoxError(PENDING if FIELD in data else REFUSED) from None
    except (OSError, ValueError, KeyError, TypeError, AttributeError, IndexError):
        raise box.BoxError(PENDING if FIELD in data else REFUSED) from None
