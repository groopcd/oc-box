"""Read-only host-side resource reporting for oc-box workspaces."""

from datetime import datetime, timezone
import json
import math
from pathlib import Path
import re
import shutil
import subprocess

if __package__:
    from . import workspace_identity
else:
    import workspace_identity

LABEL = "io.oc-box.workspace"


def byte_size(value):
    if value is None:
        return "n/a"
    size = float(value)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB", "PiB"):
        if abs(size) < 1024 or unit == "PiB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024


def duration(seconds):
    if seconds is None:
        return "-"
    minutes, seconds = divmod(int(seconds), 60)
    hours, minutes = divmod(minutes, 60)
    days, hours = divmod(hours, 24)
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m {seconds}s"


def number(value):
    if (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
        and value >= 0
    ):
        return value
    return None


def memory_limit(value):
    match = re.fullmatch(
        r"(\d+(?:\.\d+)?)\s*([kmgtp]?)(?:i?b)?", str(value), re.IGNORECASE
    )
    if match:
        return int(float(match[1]) * 1024 ** " kmgtp".index(match[2].lower() or " "))
    return None


def uptime(started, now):
    try:
        # Podman uses RFC3339Nano; Python 3.10 accepts only fixed-width
        # fractional seconds. Normalize to microseconds before parsing.
        started = re.sub(
            r"\.(\d+)", lambda match: "." + match[1][:6].ljust(6, "0"), started, count=1
        )
        timestamp = datetime.fromisoformat(started.replace("Z", "+00:00"))
        if timestamp.year > 1:
            return max(0, int((now - timestamp).total_seconds()))
    except (AttributeError, TypeError, ValueError):
        pass
    return None


def directory_bytes(path, *, private=False):
    """du counts allocated blocks, never follows symlinks or crosses mounts.

    Enter the rootless user namespace for a private home: packages installed as
    other container UIDs can create directories inaccessible to the host UID.
    No process is executed inside a managed container.
    """
    command = [
        "du",
        "--summarize",
        "--one-file-system",
        "--block-size=1",
        "--",
        str(path),
    ]
    if private:
        command = ["podman", "unshare", *command]
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=15)
        if result.returncode:
            return (
                None,
                "size scan failed (access denied, files changed, or path unavailable)",
            )
        return int(result.stdout.split()[0]), None
    except subprocess.TimeoutExpired:
        return None, "size scan exceeded 15s"
    except (OSError, ValueError, IndexError):
        return None, "size scan unavailable"


def filesystem_usage(path):
    try:
        usage = shutil.disk_usage(path)
        return {
            "total_bytes": usage.total,
            "used_bytes": usage.used,
            "free_bytes": usage.free,
        }
    except OSError:
        return None


def project_condition(data):
    """Use both the directory identity and marker, without starting Podman."""
    project = Path(data["project"])
    try:
        project.stat()
        if not project.is_dir() or project.is_symlink():
            return "replaced directory", None
        marker = project / ".oc-box-id"
        if marker.is_symlink() or (marker.exists() and not marker.is_file()):
            return "invalid marker", None
        selected = marker.read_text().strip() if marker.is_file() else None
        if selected is not None and not re.fullmatch(r"[0-9a-f]{32}", selected):
            return "invalid marker", None
        if selected is not None and selected != data["id"]:
            return "marker mismatch", selected
        if selected is None:
            return "marker missing", None
        if not workspace_identity.same_project(data, project):
            return "identity changed", selected
        return "present", selected
    except FileNotFoundError:
        return "missing directory", None
    except UnicodeError:
        return "invalid marker", None
    except OSError:
        return "inaccessible", None


def inspect_resource(podman, kind, name, owner, notes, *, size=False):
    command = [kind, "inspect", *(["--size"] if size else []), name]
    result = podman(*command, capture=True, check=False, timeout=20)
    if result.returncode and size:
        notes.append(
            "Container layer size unavailable; using inspection without --size."
        )
        result = podman(kind, "inspect", name, capture=True, check=False, timeout=20)
    if result.returncode:
        exists = podman(kind, "exists", name, capture=True, check=False, timeout=10)
        if exists.returncode == 1:
            return None
        raise ValueError(f"Cannot inspect {kind} {name}.")
    value = json.loads(result.stdout)[0]
    labels = (
        value.get("Config", {}).get("Labels")
        if kind == "container"
        else value.get("Labels")
    )
    if (labels or {}).get(LABEL) != owner:
        raise ValueError(
            f"{kind.capitalize()} ownership label mismatch; resource not measured."
        )
    return value


def collect_workspace(path, data, *, podman, session_state, now):
    notes = []
    state = session_state(path)
    report = {
        "workspace_id": data["id"],
        "distro": data["distro"],
        "project": data["project"],
        "project_status": "missing directory",
        "container_name": data["container"],
        "container_id": None,
        "state": "missing",
        "image": data["image"],
        "storage_driver": None,
        "devices": data.get("devices", []),
        "podman_args": data.get("podman_args", []),
        "created_at": None,
        "started_at": None,
        "uptime_seconds": None,
        "exit_code": None,
        "oom_killed": None,
        "health": None,
        "restarts": None,
        "limits": {
            "cpu_cores": data.get("cpus"),
            "memory_bytes": memory_limit(data.get("memory", "")),
            "pids": data.get("pids"),
        },
        "metrics": None,
        "storage": {
            "writable_layer_bytes": None,
            "rootfs_bytes": None,
            "private_home_bytes": None,
            "private_home_state": "missing",
            "project_bytes": None,
            "project_filesystem": None,
            "home_filesystem": None,
        },
        "session_id": state.get("last"),
        "recorded_sessions": len(state.get("sessions", [])),
        "host_archive_id": state.get("host_sessions", {}).get(state.get("last")),
        "notes": notes,
    }
    project = Path(data["project"])
    condition, selected = project_condition(data)
    report["project_status"] = condition
    report["project_marker_workspace"] = selected
    report["related_workspace_ids"] = []
    if condition == "present":
        size, error = directory_bytes(project)
        report["storage"]["project_bytes"] = size
        report["storage"]["project_filesystem"] = filesystem_usage(project)
        if error:
            notes.append("Project: " + error)
    elif condition in ("identity changed", "marker missing"):
        notes.append(
            f"Project {condition}; directory not measured. Exit/stop this workspace and run: oc-box repair --workspace {data['id']}. Do not delete the marker or create a new workspace."
        )
    elif condition == "marker mismatch":
        notes.append(
            f"Project marker selects workspace {selected}; this entry may be stale. Review its sessions before plain 'oc-box purge --workspace {data['id']}' to retain project files and host history. Do not use --all or --delete-sessions for duplicate cleanup."
        )
    elif condition not in ("missing directory",):
        notes.append(
            "Project identity/marker cannot be trusted; directory not measured. Check the original project and registry before repair."
        )

    try:
        info = inspect_resource(
            podman, "container", data["container"], data["id"], notes, size=True
        )
        if info:
            current = info.get("State", {})
            limits = info.get("HostConfig", {})
            cpu = number(limits.get("NanoCpus"))
            if cpu:
                report["limits"]["cpu_cores"] = cpu / 1e9
            elif number(limits.get("CpuQuota")) and number(limits.get("CpuPeriod")):
                report["limits"]["cpu_cores"] = limits["CpuQuota"] / limits["CpuPeriod"]
            elif "NanoCpus" in limits or "CpuQuota" in limits:
                report["limits"]["cpu_cores"] = None
            for key, field in (("memory_bytes", "Memory"), ("pids", "PidsLimit")):
                if field in limits:
                    report["limits"][key] = number(limits[field])
            report.update(
                {
                    "container_id": info.get("Id", info.get("ID")),
                    "state": "paused"
                    if current.get("Paused")
                    else current.get(
                        "Status", "running" if current.get("Running") else "stopped"
                    ),
                    "image": info.get("ImageName") or data["image"],
                    "storage_driver": info.get("Driver"),
                    "created_at": info.get("Created"),
                    "started_at": current.get("StartedAt"),
                    "uptime_seconds": uptime(current.get("StartedAt"), now)
                    if current.get("Running")
                    else None,
                    "exit_code": current.get("ExitCode")
                    if not current.get("Running")
                    else None,
                    "oom_killed": current.get("OOMKilled"),
                    "health": (
                        current.get("Health") or current.get("Healthcheck") or {}
                    ).get("Status"),
                    "restarts": info.get("RestartCount"),
                }
            )
            report["storage"]["writable_layer_bytes"] = number(info.get("SizeRw"))
            report["storage"]["rootfs_bytes"] = number(info.get("SizeRootFs"))
    except (RuntimeError, ValueError, KeyError, IndexError, TypeError) as exc:
        report["state"] = "unavailable"
        notes.append(str(exc))

    try:
        volume = inspect_resource(
            podman, "volume", data["home_volume"], data["id"], notes
        )
        if volume:
            report["storage"]["private_home_state"] = "present"
            mount = volume.get("Mountpoint")
            if mount:
                size, error = directory_bytes(mount, private=True)
                report["storage"]["private_home_bytes"] = size
                report["storage"]["home_filesystem"] = filesystem_usage(mount)
                if error:
                    notes.append("Private home: " + error)
    except (RuntimeError, ValueError, KeyError, IndexError, TypeError) as exc:
        report["storage"]["private_home_state"] = "unavailable"
        notes.append(str(exc))
    return report


def collect_reports(workspaces, *, podman, session_state):
    now = datetime.now(timezone.utc)
    reports = [
        collect_workspace(
            path, data, podman=podman, session_state=session_state, now=now
        )
        for path, data in workspaces
    ]
    for item in reports:
        related = [
            other["workspace_id"]
            for other in reports
            if other["project"] == item["project"]
            and other["workspace_id"] != item["workspace_id"]
        ]
        item["related_workspace_ids"] = related
        if related:
            item["notes"].append(
                "Multiple workspaces reference this project: "
                + ", ".join(related)
                + ". The project marker selects "
                + (item["project_marker_workspace"] or "none")
                + "; no entries were automatically deleted."
            )
    running = [
        item for item in reports if item["state"] == "running" and item["container_id"]
    ]
    if running:
        try:
            result = podman(
                "stats",
                "--no-stream",
                "--no-reset",
                "--no-trunc",
                "--format",
                "{{json .ContainerStats}}",
                *(item["container_id"] for item in running),
                capture=True,
                timeout=20,
            )
            samples = [
                json.loads(line) for line in result.stdout.splitlines() if line.strip()
            ]
            samples = {sample["ContainerID"]: sample for sample in samples}
            for item in running:
                sample = samples.get(item["container_id"])
                if sample is None:
                    item["notes"].append(
                        "Live metrics unavailable; container may have stopped during sampling."
                    )
                    continue
                item["metrics"] = {
                    key: number(sample.get(field))
                    for key, field in (
                        ("cpu_percent", "CPU"),
                        ("memory_bytes", "MemUsage"),
                        ("memory_limit_bytes", "MemLimit"),
                        ("memory_percent", "MemPerc"),
                        ("pids", "PIDs"),
                        ("block_read_bytes", "BlockInput"),
                        ("block_write_bytes", "BlockOutput"),
                    )
                }
        except (RuntimeError, ValueError, KeyError, TypeError) as exc:
            for item in running:
                item["notes"].append("Live metrics unavailable: " + str(exc))
    return {"sampled_at": now.isoformat(), "workspaces": reports}


def print_table(headers, rows):
    values = [headers, *rows]
    widths = [max(len(row[column]) for row in values) for column in range(len(headers))]
    for row in values:
        print(
            "  ".join(value.ljust(width) for value, width in zip(row, widths)).rstrip()
        )


def print_report(report):
    items = report["workspaces"]
    if not items:
        print("No oc-box workspaces registered.")
        return
    print("Resource snapshot: " + report["sampled_at"])
    rows = []
    for item in items:
        metrics = item["metrics"] or {}
        memory = metrics.get("memory_bytes")
        limit = metrics.get("memory_limit_bytes") or item["limits"]["memory_bytes"]
        cpu = metrics.get("cpu_percent")
        rows.append(
            (
                item["workspace_id"],
                item["distro"],
                item["state"],
                f"{cpu:.1f}%" if cpu is not None else "-",
                f"{byte_size(memory)} / {byte_size(limit) if limit else 'unlimited'}",
                str(metrics["pids"]) if metrics.get("pids") is not None else "-",
                duration(item["uptime_seconds"]),
            )
        )
    print_table(
        ("WORKSPACE ID", "DISTRO", "STATE", "CPU", "RAM / LIMIT", "PIDS", "UPTIME"),
        rows,
    )
    print()
    print_table(
        ("WORKSPACE ID", "WRITABLE OS", "PRIVATE HOME", "PROJECT"),
        [
            (
                item["workspace_id"],
                *(
                    byte_size(item["storage"][key])
                    for key in (
                        "writable_layer_bytes",
                        "private_home_bytes",
                        "project_bytes",
                    )
                ),
            )
            for item in items
        ],
    )
    for item in items:
        print(f"\n{item['workspace_id']}")
        print(f"  Project: {item['project']} ({item['project_status']})")
        print(
            f"  Container: {item['container_name']} ({item['container_id'] or 'missing'})"
        )
        print(
            f"  Image: {item['image']} | Storage driver: {item['storage_driver'] or 'n/a'}"
        )
        print(
            f"  Limits: {item['limits']['cpu_cores'] or 'unlimited'} CPUs | {item['limits']['pids'] or 'unlimited'} processes"
        )
        print("  Devices: " + (", ".join(item["devices"]) or "none"))
        print(
            "  Additional Podman options: " + (" ".join(item["podman_args"]) or "none")
        )
        print(
            f"  Session: {item['session_id'] or 'none'} | Recorded sessions: {item['recorded_sessions']}"
        )
        print(f"  Host archive: {item['host_archive_id'] or 'not recorded'}")
        print(
            f"  Created: {item['created_at'] or 'n/a'} | Restarts: {item['restarts'] if item['restarts'] is not None else 'n/a'}"
        )
        metrics = item["metrics"] or {}
        print(
            f"  Block I/O read/write: {byte_size(metrics.get('block_read_bytes'))} / {byte_size(metrics.get('block_write_bytes'))}"
        )
        print(
            f"  Rootfs including shared image layers: {byte_size(item['storage']['rootfs_bytes'])}"
        )
        for key, label in (
            ("project_filesystem", "Project filesystem"),
            ("home_filesystem", "Podman home filesystem"),
        ):
            disk = item["storage"][key]
            if disk:
                print(
                    f"  {label}: {byte_size(disk['used_bytes'])} used / {byte_size(disk['total_bytes'])} total; {byte_size(disk['free_bytes'])} free"
                )
        if item["exit_code"] is not None:
            print(f"  Last exit: {item['exit_code']}")
        if item["oom_killed"]:
            print("  Last exit involved an out-of-memory kill.")
        if item["health"]:
            print(f"  Health: {item['health']}")
        for note in item["notes"]:
            print("  Note: " + note)
    print(
        "\nCPU is a sampled percentage (100% = one CPU); stopped containers have no live sample."
    )
    print(
        "Storage sizes exclude shared image layers from WRITABLE OS; Btrfs CoW/compression can affect physical accounting."
    )
    print(
        "Network counters are omitted because rootless pasta networking does not reliably expose them."
    )
