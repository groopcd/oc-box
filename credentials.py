"""Opt-in per-workspace selection; default sourced-environment behavior remains.

No values belong in workspace metadata. Selection controls env forwarding only,
not secrets previously copied in OpenCode config/auth, files, or other processes.
"""

import contextlib
from contextvars import ContextVar
import os

if __package__:
    from . import vault
else:
    import vault

_UNLOCKED: ContextVar[tuple[str, dict[str, str]] | None] = ContextVar(
    "oc_box_vault_launch", default=None
)


def policy(data):
    value = data.get("credentials", {"mode": "env", "env": [], "vault": []})
    if (
        not isinstance(value, dict)
        or set(value) != {"mode", "env", "vault"}
        or value["mode"] not in ("env", "selected")
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


def configure(data, *, mode=None, env=(), secrets=(), clear=False):
    result = policy(data)
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
def unlocked(data):
    selected = policy(data)
    current = _UNLOCKED.get()
    if current is not None and current[0] == data["id"]:
        yield current[1]
        return
    # Legacy env mode may opt in selected vault names too; env source remains
    # broad by design. Never unlock for default/no-selection launches.
    names = selected["vault"]
    if names and os.environ.get("OC_BOX_CONTAINER") == "1":
        raise vault.VaultError(
            "Unlock the credential vault from the host, not from a development container."
        )
    if selected["mode"] == "selected":
        missing = [name for name in selected["env"] if not os.environ.get(name)]
        if missing:
            raise vault.VaultError(
                "Selected environment credentials are unset/empty. Source your host env file or update 'oc-box credentials': "
                + ", ".join(missing)
            )
    values = vault.select(names) if names else {}
    try:
        token = _UNLOCKED.set((data["id"], values))
        try:
            yield values
        finally:
            _UNLOCKED.reset(token)
    finally:
        # Best effort reference removal only. Python/OS do not guarantee
        # zeroization; container processes keep delivered env values normally.
        values.clear()


def forwarded(data, legacy):
    selected = policy(data)
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
        result.update(
            {name: os.environ[name] for name in selected["env"] if name in os.environ}
        )
    current = _UNLOCKED.get()
    if current is not None and current[0] == data["id"]:
        result.update(current[1])
    return result


def active_values(data):
    current = _UNLOCKED.get()
    return current[1] if current is not None and current[0] == data["id"] else {}


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
