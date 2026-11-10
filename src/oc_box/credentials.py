"""Launch-local credential selection and native V2 global vault defaults.

No values belong in workspace metadata. Selection controls env forwarding only,
not secrets previously copied in OpenCode config/auth, files, or other processes.
"""

import contextlib
from contextvars import ContextVar
import os
import struct
import sys

if __package__:
    from . import opencode_compat, vault
else:
    import opencode_compat
    import vault

# This public cap is only an upper bound; actual exec limits can be smaller.
# Explicit per-name policies retain their existing selection cap semantics.
MAX_GLOBAL_ENV_BYTES = 1024 * 1024
EXEC_ARGV_HEADROOM = 128 * 1024
EXEC_ARGV_POINTER_HEADROOM = 256

_UNLOCKED: ContextVar[tuple[str, dict[str, str]] | None] = ContextVar(
    "oc_box_vault_launch", default=None
)
_EFFECTIVE_POLICY: ContextVar[tuple[str, dict] | None] = ContextVar(
    "oc_box_credential_policy", default=None
)


def _major(data, version=None):
    release = data.get("opencode_version", "1.0.0") if version is None else version
    try:
        return opencode_compat.major(release)
    except (AttributeError, TypeError, ValueError) as exc:
        raise vault.VaultError(
            "Unsupported OpenCode version; supported release series are 1.x and 2.x."
        ) from exc


def _policy_value(data, version=None):
    return (
        data.get("credentials")
        if "credentials" in data
        else {
            "mode": "vault" if _major(data, version) == 2 else "env",
            "env": [],
            "vault": [],
        }
    )


def _validated_policy(value, *, allow_global_selections=False):
    if (
        not isinstance(value, dict)
        or set(value) != {"mode", "env", "vault"}
        or value["mode"] not in ("env", "selected", "vault")
    ):
        raise vault.VaultError(
            "Invalid workspace credential policy; use 'oc-box credentials' on the host."
        )
    for source in ("env", "vault"):
        if (
            not isinstance(value[source], list)
            or len(value[source]) > vault.MAX_ENTRIES
        ):
            raise vault.VaultError("Invalid credential selection list.")
    if (
        value["mode"] == "vault"
        and (value["env"] or value["vault"])
        and not allow_global_selections
    ):
        raise vault.VaultError(
            "Global vault mode cannot contain credential selection lists. Use selected/env mode, "
            "or --clear-credentials when switching to vault mode."
        )
    for source in ("env", "vault"):
        for name in value[source]:
            vault.validate_name(name)
    if set(value["env"]) & set(value["vault"]):
        raise vault.VaultError(
            "Select each credential from either env or vault, not both."
        )
    return {
        key: list(names) if isinstance(names, list) else names
        for key, names in value.items()
    }


def policy(data, *, version=None):
    return _validated_policy(_policy_value(data, version))


def configure(data, *, mode=None, env=(), secrets=(), clear=False):
    # Explicit repair may bypass only the known mode/list incompatibility.
    # Still validate the complete saved shape, every name and source conflict
    # before clearing anything; unknown malformed data must remain untouched.
    result = _validated_policy(
        _policy_value(data),
        allow_global_selections=clear or mode in ("env", "selected"),
    )
    if clear:
        result["env"], result["vault"] = [], []
    for source, names in (("env", env), ("vault", secrets)):
        for name in names:
            vault.validate_name(name)
            if name not in result[source]:
                result[source].append(name)
    if mode is not None:
        result["mode"] = mode
    updated = {**data, "credentials": result}
    result = policy(updated)
    data["credentials"] = result
    return result


@contextlib.contextmanager
def unlocked(data, *, version=None, no_vault=False):
    """Unlock once per owner; an outer same-owner launch policy stays authoritative."""
    selected = policy(data, version=version)
    major = _major(data, version)
    if no_vault and major != 2:
        raise vault.VaultError("--no-vault requires native OpenCode 2.x.")
    current = _UNLOCKED.get()
    if current is not None and current[0] == data["id"]:
        yield current[1]
        return
    # A saved global policy on V1 uses the unchanged legacy environment. An
    # explicit V2 opt-out is launch-local, including selected-vault omissions.
    if selected["mode"] == "vault" and (major == 1 or no_vault):
        selected["mode"] = "env"
    if no_vault:
        selected["vault"] = []
    global_vault = selected["mode"] == "vault"
    names = selected["vault"]
    if (global_vault or names) and os.environ.get("OC_BOX_CONTAINER") == "1":
        raise vault.VaultError(
            "Unlock the credential vault from the host, not from a development container."
        )
    if selected["mode"] == "selected":
        missing = [name for name in selected["env"] if not os.environ.get(name)]
        if missing:
            raise vault.VaultError(
                "Selected environment credentials are unset/empty. Source your host env file or update 'oc-box credentials'."
            )
    if global_vault:
        if vault.is_present():
            values = vault.select_all()
            if not values:
                _setup_guidance(initialized=True)
        else:
            # Presence is metadata-only. No crypto, prompt, installation or
            # config/env migration is needed for this compatibility fallback.
            selected["mode"] = "env"
            values = {}
            _setup_guidance(initialized=False)
    else:
        # Explicit env/selected policies retain their named-vault semantics.
        values = vault.select(names) if names else {}
    try:
        if global_vault:
            _check_global_env_budget(values)
        token = _UNLOCKED.set((data["id"], values))
        try:
            policy_token = _EFFECTIVE_POLICY.set((data["id"], selected))
            try:
                if global_vault or values:
                    _check_exec_budget(data, version=version)
                yield values
            finally:
                _EFFECTIVE_POLICY.reset(policy_token)
        finally:
            _UNLOCKED.reset(token)
    finally:
        # Best effort reference removal only. Python/OS do not guarantee
        # zeroization; container processes keep delivered env values normally.
        values.clear()


def _check_global_env_budget(values):
    # Each exec environment entry is UTF-8 NAME=value followed by NUL. Finish
    # the generator before raising so its frame retains no individual values.
    size = sum(
        len(name.encode("utf-8")) + len(value.encode("utf-8")) + 2
        for name, value in values.items()
    )
    if size > MAX_GLOBAL_ENV_BYTES:
        raise vault.VaultError(
            "Global vault environment size limit exceeded (1 MiB). Use selected mode with fewer credentials."
        )


def _check_exec_budget(data, *, version=None):
    """Conservative launch preflight, not a guarantee about future execs.

    Build the normal commands/environments without starting any process. Count
    both Podman's host environment and the delivered container environment; the
    former still includes ambient variables even in global/selected mode. Leave
    room for runtime settings, inline helper code, additional argv and pointers.
    Never modify host os.environ or retain these extra plaintext dictionaries.
    """

    def refuse():
        raise vault.VaultError(
            "Credential launch exec budget/limit exceeded or unavailable. "
            "Reduce the inherited host environment or select fewer credentials; "
            "check the host stack/exec limits before retrying."
        ) from None

    try:
        maximum = os.sysconf("SC_ARG_MAX")
        page_size = os.sysconf("SC_PAGESIZE")
    except (OSError, ValueError):
        refuse()
    if (
        type(maximum) is not int
        or type(page_size) is not int
        or min(maximum, page_size) <= 0
    ):
        refuse()
    # Linux bounds each argv/environment string to 32 pages, including NUL.
    per_string = 32 * page_size
    if __package__:
        from . import oc_box as box
    else:
        import oc_box as box
    planned = dict(data)
    if version is not None:
        planned["opencode_version"] = version
    environments = [dict(os.environ)]
    try:
        command = box.exec_command(planned, ["/bin/true"])
        environments.append(command.environment)
        environments.append(box.container_env(planned))
        # These production helpers are passed as single Python -c arguments.
        helpers = [
            (box.PACKAGE / name).stat().st_size + 1
            for name in ("bootstrap.py", "sync_config.py", "opencode_v2_io.py")
        ]
        arguments = [len(os.fsencode(str(arg))) + 1 for arg in command]
        if max([*helpers, *arguments], default=0) > per_string:
            refuse()
        argv_bytes = sum(arguments) + max(helpers) + EXEC_ARGV_HEADROOM
        pointers = struct.calcsize("P") * (
            len(arguments) + EXEC_ARGV_POINTER_HEADROOM + 2
        )
        for environment in environments:
            sizes = [
                len(os.fsencode(name)) + len(os.fsencode(value)) + 2
                for name, value in environment.items()
            ]
            if max(sizes, default=0) > per_string:
                refuse()
            if (
                sum(sizes) + argv_bytes + pointers + struct.calcsize("P") * len(sizes)
                > maximum
            ):
                refuse()
    except (OSError, UnicodeError):
        refuse()
    finally:
        for environment in environments:
            environment.clear()


def _setup_guidance(*, initialized):
    if initialized:
        status = "Global vault is empty; no vault credentials will be forwarded."
        commands = "oc-box doctor --feature vault; oc-box secrets set NAME"
    else:
        status = "Global vault is not initialized; using env-file forwarding for this launch."
        commands = "oc-box doctor --feature vault; oc-box secrets init; oc-box secrets set NAME"
    print(status + "\nRecommended setup: " + commands, file=sys.stderr)


def effective_policy(data, *, version=None):
    """Return a private copy of this owner's launch policy, or its saved default.

    Unlike policy(), this reflects a scoped opt-out or absent-vault fallback.
    Outside a launch, no presence check or unlock is performed.
    """
    effective = _EFFECTIVE_POLICY.get()
    if effective is not None and effective[0] == data["id"]:
        selected = effective[1]
    else:
        selected = policy(data, version=version)
        if selected["mode"] == "vault" and _major(data, version) == 1:
            selected["mode"] = "env"
    return {
        key: list(names) if isinstance(names, list) else names
        for key, names in selected.items()
    }


def forwarded(data, legacy):
    selected = effective_policy(data)
    if selected["mode"] == "env":
        result = dict(legacy)
    else:
        # Terminal/provider routing preferences are not credentials. The
        # allowlist replaces ALL auto _TOKEN/_API_KEY and custom pass_env names.
        result = {
            key: value
            for key, value in legacy.items()
            if key in ("COLORTERM", "LANG", "LEMONADE_BASE_URL")
        }
        if selected["mode"] == "selected":
            result.update(
                {
                    name: os.environ[name]
                    for name in selected["env"]
                    if name in os.environ
                }
            )
    current = _UNLOCKED.get()
    if current is not None and current[0] == data["id"]:
        result.update(current[1])
    return result


def active_values(data):
    current = _UNLOCKED.get()
    return current[1] if current is not None and current[0] == data["id"] else {}


def is_active(data):
    """Whether this owner already entered a credential context, even if empty."""
    current = _UNLOCKED.get()
    return current is not None and current[0] == data["id"]


def redact(message):
    current = _UNLOCKED.get()
    if current is not None:
        for value in sorted(current[1].values(), key=len, reverse=True):
            if value:
                message = message.replace(value, "[redacted credential]")
    return message


def has_unlocked_values():
    current = _UNLOCKED.get()
    return current is not None and bool(current[1])
