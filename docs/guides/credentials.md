# Deliver credentials deliberately

[Documentation](../README.md) / Guides

> **OpenCode 2.x credential policy.** Legacy 1.x uses different defaults; its
> [credential guide](../legacy/README.md#credentials) is kept separately.

Create an encrypted host vault, choose what a workspace receives, and understand
what changing that policy does—and does not—revoke.

**Prerequisites:** an [installation](../installation.md), a host controlling
terminal, and optional vault support for encrypted storage. These workflows are
available in stable 1.6.94; the vault extra is independent of the experimental
dashboard and of [release-channel selection](../update-channels.md).

**Contents:** [default policy](#understand-the-default-policy),
[vault setup](#enable-vault-support-and-store-secrets),
[private prompts](#enter-and-protect-secret-values),
[workspace overrides](#restrict-an-existing-workspace),
and [one-launch compatibility](#make-one-v2-env-file-compatibility-launch).

## Understand the default policy

**V2 defaults to global vault-first without an explicit workspace policy.** This
includes existing V2 workspaces with no policy; saved env/selected policies remain
intentional overrides. Global mode uses
all current entries on each normal new owner, including the first launch, rather
than saving a frozen automatic name list. Additions/removals and updated values
apply on the next new owner, not to attached TUI/web/session-shell clients.

When the host vault is present, all current entries replace ambient automatic
token/API-key and custom `pass_env` forwarding, including legacy
`OPENCODE_AUTH_CONTENT` overrides. An explicit env override or V2 without a
vault retains env compatibility forwarding; selected mode restricts credential
names.

Ordinary native OpenCode saved/OAuth credential refresh and copying continue in
every mode. Vault env entries do not replace structured OAuth refresh-token
records: a vault containing only a search API key does not disable an existing
Copilot OAuth account. No plaintext OpenCode JSON secrets are imported or
rewritten. Existing copied/container auth/config values remain independent;
no revocation is implied.

**Global use deliberately exposes every stored entry to each default V2
workspace; use selected mode when a project should receive less.** Delivered
credentials remain readable by the agent/tools, and copied config/auth may
already contain secrets. Forwarded vault values never enter Podman argv, host
`os.environ`, config or registry. The vault/passphrase are never mounted into
the container. See [security](../../SECURITY.md).

## Enable vault support and store secrets

```sh
# New isolated Python-tool installation:
pipx install 'oc-box[vault]'
# Existing pipx installation:
pipx inject oc-box 'cryptography>=44'
# Or uv (install/reinstall with the extra):
uv tool install --force 'oc-box[vault]'
# GitHub/source installer:
python3 install.py --with-vault
# Or one-line GitHub installer:
curl -fsSL --proto '=https' --tlsv1.2 https://raw.githubusercontent.com/groopcd/oc-box/master/install.sh | sh -s -- --with-vault
```

Choose the recipe for your installation owner; these examples do not select
the development channel. The GitHub option installs cryptography in the versioned
launcher's private runtime directory using a temporary virtual environment,
never system Python or user site-packages. GitHub upgrades preserve this opt-in.
There is no password-manager daemon or desktop keyring requirement. The host
needs a controlling terminal and normal private Unix file permissions, including
under WSL. Do not put the vault on an exposed project mount or a Windows-shared
path that cannot enforce those permissions.

Create the vault and store secrets **before your first V2 project launch** using
interactive masked prompts (names only in argv). Global use needs no per-project
registration, exit/configure/relaunch step, or selection command:

```sh
oc-box secrets init
oc-box secrets set ANTHROPIC_API_KEY
oc-box secrets set MCP_API_KEY
oc-box secrets set PERSONAL_GITHUB_TOKEN
oc-box secrets set BRAVE_API_KEY
oc-box secrets list            # Unlocks; prints names, never values
oc-box secrets remove OLD_TOKEN
oc-box secrets passwd          # Change the vault passphrase
```

When no vault exists, default V2 launches keep env-file fallback and concise
setup guidance. They do not automatically install crypto, create a vault, modify
config, or import JSON secrets. An empty initialized vault still unlocks and
delivers no vault entries, with guidance to run `oc-box secrets set NAME`.
**A present corrupt vault, wrong passphrase, or missing crypto support aborts
before container setup/reset deletion, without silently falling back.**

## Enter and protect secret values

Typing/pasting shows `*` characters and a live character count. At **secret
value** and confirmation prompts, press **Ctrl+R** to switch between masked and
visible input; each prompt starts masked. Vault passphrases always stay masked
and cannot be revealed. Use Backspace/Delete and arrow/Home/End keys to correct
input, **Ctrl+U** to clear, Enter to accept, or **Ctrl+C** to cancel. Bracketed
multiline/control-character or oversized pastes are rejected rather than
silently truncated. The input field scrolls horizontally for long values.

Revealed text is cleared when leaving the prompt, but explicitly showing a key
can expose it in terminal recordings, screenshots, scrollback/reflow, or screen
sharing. Masked feedback reveals length, not content. There is still no plaintext
value command-line argument, saved history, or change to the encrypted format.

The host vault is normally `~/.local/share/oc-box/credentials/vault.json`
(`$XDG_DATA_HOME/oc-box/credentials/vault.json` when set), outside project and
container storage. Credentials and their names are encrypted with AES-256-GCM;
Scrypt derives the key from a passphrase (default minimum 8 characters). The
private file is 0600 and its directory 0700. There is no saved key, unlock cache,
passphrase environment variable, plaintext export, or recovery mechanism for a
forgotten passphrase. Back up the encrypted vault and keep its passphrase safe.

Set separate minimum lengths in `~/.config/oc-box/config.json` (or its XDG
equivalent), retaining any existing settings:

```json
{
  "vault_min_passphrase_length": 12,
  "backup_min_passphrase_length": 16
}
```

Both default to 8 and accept integers from 8 to 1024; passwords can be longer
than the selected minimum, up to 1024 UTF-8 bytes. Override for one operation
with `oc-box secrets init --min-passphrase-length 8`,
`oc-box secrets passwd --min-passphrase-length 8`, or
`oc-box backup ... --min-passphrase-length 8`. These rules apply to new/changed
passwords; existing vaults/backups still unlock with their original passwords.

## Restrict an existing workspace

After exiting its active launcher, select only what an **existing** workspace
needs. These commands do not start/reset a container or read values:

```sh
oc-box credentials --project ~/devel/demo
oc-box credentials --project ~/devel/demo --credential-mode selected \
  --clear-credentials --vault-secret PERSONAL_GITHUB_TOKEN --vault-secret BRAVE_API_KEY
oc-box --project ~/devel/demo
```

Selected mode replaces automatic `_TOKEN`/`_API_KEY` and custom `pass_env`
forwarding with those explicit names. It permits mixing sourced environment
credentials and selected vault entries, but not selecting the same name from
both. Normal owning start/reset/import/web prompts once to unlock selected vault
names; the passphrase and vault file never enter the container. A missing selected
env variable, wrong passphrase, or missing vault entry aborts before container
setup or reset deletion. `terminal`, list/status, repair, stop, upgrade, and
explicit history sync never unlock the vault. Delivered credentials are still
environment variables accessible to OpenCode, its tools, and other permitted
processes.

Selections persist in the host registry as **names only**, independent of the
project and vault. Repeating options appends names; replace them with
`--clear-credentials`. Accepted modes are `env`, `selected`, and `vault`.
Vault mode uses all current entries, refuses nonempty per-name selections, and
does not save an automatic list. Switch an old selection back to global V2:

```sh
oc-box credentials --project ~/devel/demo --credential-mode vault --clear-credentials
```

For a deliberate persistent legacy env override:

```sh
oc-box credentials --project ~/devel/demo --credential-mode env --clear-credentials
```

Env mode can also add selected vault names without restricting existing env
forwarding. A selected vault value wins over an inherited same-named env value
in that mode. Switching mode does **not** remove old secrets from the private
home, copied OpenCode auth/config, raw Podman `--env` settings, other running
processes, source files, session history, swap, or backups. The vault protects
storage before delivery; it does not hide a delivered key from the agent.
Read the [credential reference](../cli.md#credentials) and
[security notes](../../SECURITY.md) before migrating sensitive credentials.

## Make one V2 env-file compatibility launch

V2-only `--no-vault` skips vault entries for **one owning start/reset/web
invocation**, without saving policy. Global mode uses legacy env forwarding for
that invocation; explicit env/selected modes retain their env constraints.
Attached clients reject it and never unlock/refresh. Information, maintenance,
sync, backup/restore, credential management and unrelated commands reject the
flag too. For one-off env-file compatibility only:

```sh
. ~/.config/provider-credentials.env
oc-box --project ~/devel/demo --no-vault
# Or plain host OpenCode, which never consumes the oc-box vault:
opencode --standalone
```

Sourcing a file is not the recommended primary V2 workflow. The next normal
oc-box owner uses its saved/default policy again. Plain host OpenCode uses
normal host env/auth, not the oc-box vault.

For retained 1.x env defaults, selected credentials and saved `vault` policies,
use the [legacy credential rules](../legacy/README.md#credentials). Do not apply
the 2.x global-vault default or `--no-vault` recipe to that runtime.

## Related

- [Secret commands](../cli.md#secrets) · [Credential policy reference](../cli.md#credentials)
- [Backups and the separately protected host vault](backups.md)
- [Core concepts and permissions](../concepts.md) · [Security](../../SECURITY.md)
