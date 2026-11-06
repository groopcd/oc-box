"""Opt-in host-local encrypted credential storage; no daemon or plaintext file.

Format v1 uses cryptography's Scrypt (128 MiB) and AES-256-GCM. Crypto is lazy
and optional so the existing environment workflow needs no new dependency.
Only ciphertext and public crypto parameters are stored. No key/passphrase is
cached, exported, put in argv, or written alongside the vault.
"""

import base64
import contextlib
import fcntl
import getpass
import json
import os
from pathlib import Path
import re
import resource
import stat
import sys
import tempfile
import warnings

FORMAT = "oc-box-vault"
VERSION = 1
KDF = {"name": "scrypt", "n": 2**17, "r": 8, "p": 1}
MAX_FILE = 2 * 1024 * 1024
MAX_SECRET = 64 * 1024
MAX_ENTRIES = 128


class VaultError(Exception):
    pass


def validate_name(name):
    if not isinstance(name, str) or not re.fullmatch(r"[A-Z_][A-Z_0-9]{0,127}", name):
        raise VaultError(
            "Credential names must be uppercase environment names, at most 128 characters."
        )
    reserved = {
        "HOME",
        "PATH",
        "PWD",
        "OLDPWD",
        "SHELL",
        "TERM",
        "LANG",
        "COLORTERM",
        "ENV",
        "BASH_ENV",
        "PROMPT_COMMAND",
        "PS1",
        "PS2",
        "PS4",
        "IFS",
        "CDPATH",
        "SHELLOPTS",
        "BASHOPTS",
        "NODE_OPTIONS",
        "NODE_PATH",
        "PERL5OPT",
        "PERL5LIB",
        "RUBYOPT",
        "RUBYLIB",
        "GEM_HOME",
        "GEM_PATH",
        "GLIBC_TUNABLES",
        "SSH_AUTH_SOCK",
        "SSH_AGENT_PID",
        "DOCKER_HOST",
        "CONTAINER_HOST",
        "DBUS_SESSION_BUS_ADDRESS",
        "TMPDIR",
        "TMP",
        "TEMP",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "NO_PROXY",
    }
    if name in reserved or name.startswith(
        (
            "OC_BOX_",
            "OPENCODE_",
            "XDG_",
            "LD_",
            "DYLD_",
            "PYTHON",
            "GIT_",
            "BASH_FUNC_",
            "LC_",
        )
    ):
        raise VaultError(
            "Credential name is reserved for runtime, loader, shell, or host control settings."
        )
    return name


def validate_value(value):
    if (
        not isinstance(value, str)
        or not value
        or "\0" in value
        or len(value.encode()) > MAX_SECRET
    ):
        raise VaultError(
            "Secret value must be nonempty UTF-8 text without NUL and at most 64 KiB."
        )


def vault_path():
    base = Path(os.environ.get("XDG_DATA_HOME", str(Path.home() / ".local/share")))
    return base.expanduser().absolute() / "oc-box/credentials/vault.json"


def crypto():
    # GitHub installer can bundle optional wheels next to this module. Package
    # installations resolve the optional dependency through pipx/uv/pip instead.
    bundled = Path(__file__).resolve().parent / "vault-deps"
    if bundled.is_dir() and str(bundled) not in sys.path:
        sys.path.insert(0, str(bundled))
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        from cryptography.hazmat.primitives.kdf.scrypt import Scrypt
        from cryptography.exceptions import InvalidTag
    except ImportError as exc:
        raise VaultError(
            "Vault support needs the optional cryptography dependency. Use pipx/uv to install 'oc-box[vault]', or rerun the GitHub/source installer with --with-vault. The default env-file workflow requires no crypto dependency."
        ) from exc
    return AESGCM, Scrypt, InvalidTag


def hidden(prompt):
    # getpass otherwise falls back to echoing input and warns on redirected
    # stdin. A controlling terminal must exist; fail rather than accept that.
    try:
        # Reduce accidental plaintext disclosure through launcher core dumps.
        # This is not memory zeroization or protection against same-user/root
        # debugging, swap, or crash collection outside normal rlimit behavior.
        try:
            _, hard = resource.getrlimit(resource.RLIMIT_CORE)
            resource.setrlimit(resource.RLIMIT_CORE, (0, hard))
        except (OSError, ValueError):
            pass
        terminal = os.open("/dev/tty", os.O_RDWR | os.O_CLOEXEC | os.O_NOCTTY)
        try:
            if not os.isatty(terminal):
                raise VaultError(
                    "Secret input requires a controlling host terminal; stdin, files, and environment passphrases are not accepted."
                )
        finally:
            os.close(terminal)
        with warnings.catch_warnings():
            warnings.simplefilter("error", getpass.GetPassWarning)
            return getpass.getpass(prompt)
    except (OSError, EOFError, getpass.GetPassWarning) as exc:
        raise VaultError(
            "Secret input requires a controlling host terminal with hidden input."
        ) from exc


def new_passphrase():
    first = hidden("New vault passphrase (12+ characters): ")
    if len(first) < 12 or "\0" in first or len(first.encode()) > 1024:
        raise VaultError(
            "Choose a passphrase of 12 or more characters (maximum 1024 UTF-8 bytes)."
        )
    if first != hidden("Confirm vault passphrase: "):
        raise VaultError("Passphrases do not match; vault was not changed.")
    return first


def private_directory(path):
    # Refuse symlinks in existing path components. The caller selected its XDG
    # location; never chmod a shared parent such as ~/.local/share itself.
    for parent in (*reversed(path.parents), path):
        if parent.is_symlink():
            raise VaultError("Vault path must not contain symbolic links.")
    path.mkdir(parents=True, mode=0o700, exist_ok=True)
    metadata = path.stat()
    if metadata.st_uid != os.getuid() or metadata.st_mode & 0o077:
        raise VaultError(
            "Vault directory must be owned by your user with permissions 0700. Fix the host directory before continuing."
        )


@contextlib.contextmanager
def locked(path, *, create=False):
    path = Path(path).absolute()
    if create:
        private_directory(path.parent)
    elif not path.parent.exists():
        raise VaultError(
            "Vault is not initialized. Run 'oc-box secrets init' on the host first."
        )
    else:
        private_directory(path.parent)
    fd = os.open(
        path.parent / ".vault.lock",
        os.O_CREAT | os.O_RDWR | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK,
        0o600,
    )
    try:
        metadata = os.fstat(fd)
        if (
            metadata.st_uid != os.getuid()
            or not stat.S_ISREG(metadata.st_mode)
            or metadata.st_mode & 0o077
            or metadata.st_nlink != 1
        ):
            raise VaultError(
                "Vault lock must be a private regular file owned by your user."
            )
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield path
    finally:
        os.close(fd)


def read_envelope(path):
    try:
        fd = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError as exc:
        raise VaultError(
            "Vault is not initialized. Run 'oc-box secrets init' on the host first."
        ) from exc
    try:
        metadata = os.fstat(fd)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.getuid()
            or metadata.st_mode & 0o077
            or metadata.st_nlink != 1
            or metadata.st_size > MAX_FILE
        ):
            raise VaultError(
                "Vault must be a private regular file (0600), owned by your user, with no extra hard links."
            )
        with os.fdopen(fd, "rb", closefd=False) as stream:
            raw = stream.read(MAX_FILE + 1)
        if len(raw) > MAX_FILE:
            raise ValueError
        envelope = json.loads(raw)
        if not isinstance(envelope, dict) or set(envelope) != {
            "format",
            "version",
            "kdf",
            "cipher",
            "ciphertext",
        }:
            raise ValueError
        if (
            envelope["format"] != FORMAT
            or type(envelope["version"]) is not int
            or envelope["version"] != VERSION
        ):
            raise ValueError
        kdf, cipher = envelope["kdf"], envelope["cipher"]
        if (
            not isinstance(kdf, dict)
            or set(kdf) != {*KDF, "salt"}
            or any(
                type(kdf[key]) is not type(value) or kdf[key] != value
                for key, value in KDF.items()
            )
        ):
            raise ValueError
        if (
            not isinstance(cipher, dict)
            or set(cipher) != {"name", "nonce"}
            or cipher["name"] != "AES-256-GCM"
        ):
            raise ValueError
        decode(kdf["salt"], length=16)
        decode(cipher["nonce"], length=12)
        decode(envelope["ciphertext"])
        return envelope
    except (ValueError, KeyError, TypeError, UnicodeError) as exc:
        raise VaultError(
            "Unsupported or damaged vault format; no data was changed."
        ) from exc
    finally:
        os.close(fd)


def encode(value):
    return base64.b64encode(value).decode("ascii")


def decode(value, length=None):
    if not isinstance(value, str):
        raise ValueError("Invalid encoded vault field")
    result = base64.b64decode(value, validate=True)
    if length is not None and len(result) != length:
        raise ValueError("Invalid vault field length")
    return result


def header(envelope):
    return json.dumps(
        {key: value for key, value in envelope.items() if key != "ciphertext"},
        sort_keys=True,
        separators=(",", ":"),
    ).encode()


def derive(passphrase, salt):
    if (
        not isinstance(passphrase, str)
        or not passphrase
        or "\0" in passphrase
        or len(passphrase.encode()) > 1024
    ):
        raise VaultError("Invalid vault passphrase length or encoding.")
    _, scrypt, _ = crypto()
    return scrypt(salt=salt, length=32, n=KDF["n"], r=KDF["r"], p=KDF["p"]).derive(
        passphrase.encode()
    )


def validate_secrets(values):
    if not isinstance(values, dict) or len(values) > MAX_ENTRIES:
        raise VaultError("Vault payload is invalid or exceeds 128 credentials.")
    for name, value in values.items():
        validate_name(name)
        validate_value(value)


def unlock(envelope, passphrase):
    aes, _, invalid_tag = crypto()
    try:
        payload = aes(derive(passphrase, decode(envelope["kdf"]["salt"], 16))).decrypt(
            decode(envelope["cipher"]["nonce"], 12),
            decode(envelope["ciphertext"]),
            header(envelope),
        )
        data = json.loads(payload)
        if not isinstance(data, dict) or set(data) != {"secrets"}:
            raise ValueError
        validate_secrets(data["secrets"])
        return data["secrets"]
    except (invalid_tag, ValueError, UnicodeError, KeyError, TypeError) as exc:
        raise VaultError(
            "Unable to unlock vault: incorrect passphrase or modified/damaged vault. No data was changed."
        ) from exc


def write_encrypted(path, values, passphrase):
    validate_secrets(values)
    aes, _, _ = crypto()
    envelope = {
        "format": FORMAT,
        "version": VERSION,
        "kdf": {**KDF, "salt": encode(os.urandom(16))},
        "cipher": {"name": "AES-256-GCM", "nonce": encode(os.urandom(12))},
    }
    payload = json.dumps({"secrets": values}, sort_keys=True).encode()
    envelope["ciphertext"] = encode(
        aes(derive(passphrase, decode(envelope["kdf"]["salt"]))).encrypt(
            decode(envelope["cipher"]["nonce"]), payload, header(envelope)
        )
    )
    output = (json.dumps(envelope, sort_keys=True) + "\n").encode()
    if len(output) > MAX_FILE:
        raise VaultError(
            "Encrypted vault exceeds the 2 MiB limit; vault was not changed."
        )
    fd, temporary = tempfile.mkstemp(prefix=".vault-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(output)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        Path(temporary).unlink(missing_ok=True)


def select(names, *, path=None):
    for name in names:
        validate_name(name)
    target = vault_path() if path is None else Path(path)
    with locked(target):
        envelope = read_envelope(target)
        crypto()
        values = unlock(envelope, hidden("Vault passphrase: "))
        if any(name not in values for name in names):
            raise VaultError(
                "One or more selected vault credential names are missing; review 'oc-box secrets list' on the host."
            )
        return {name: values[name] for name in names}


def manage(action, name=None, *, path=None):
    if action in ("set", "remove"):
        validate_name(name)
    target = vault_path() if path is None else Path(path)
    with locked(target, create=action == "init"):
        if action == "init":
            if target.exists() or target.is_symlink():
                raise VaultError(
                    "Vault already exists; initialization will not overwrite it."
                )
            crypto()
            write_encrypted(target, {}, new_passphrase())
            print(
                "Encrypted vault initialized. Keep its passphrase safe; there is no recovery key."
            )
            return
        envelope = read_envelope(target)
        crypto()
        passphrase = hidden("Vault passphrase: ")
        values = unlock(envelope, passphrase)
        if action == "list":
            print("NAME")
            for entry in sorted(values):
                print(entry)
            return
        if action == "set":
            value = hidden("Secret value (hidden): ")
            validate_value(value)
            if value != hidden("Confirm secret value: "):
                raise VaultError("Secret values do not match; vault was not changed.")
            values[name] = value
        elif action == "remove":
            if name not in values:
                raise VaultError(
                    "Credential name is not present; vault was not changed."
                )
            del values[name]
        elif action == "passwd":
            passphrase = new_passphrase()
        else:
            raise VaultError("Unknown vault operation.")
        write_encrypted(target, values, passphrase)
        print("Encrypted vault updated.")
