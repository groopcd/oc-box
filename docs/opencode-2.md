# OpenCode 1.x and 2.x compatibility

Available in oc-box 1.5.x, with retained OpenCode 1.x support. Validation uses
unmodified OpenCode 1.19.5 and 2.2.6, plus accepted disposable Linux host/rootless
Podman lifecycle tests for the earlier selected-vault workflow. The new global
vault default and `--no-vault` still need live host/rootless-Podman acceptance;
unit/native offline checks are not proof of that lifecycle. Upgrade through your
normal GitHub/curl, uv, pipx or virtual-environment package channel to get the
1.5 release.

## Environment and service plan

OpenCode 2.x still supports environment variables and `{env:NAME}` substitution.
Providers, configuration, plugins, and MCP servers run in the service: connecting
a terminal with a different environment does not replace a running service's
environment.

oc-box owns a private authenticated stdio server in each active workspace.
The full TUI and web clients use native APIs against that same service. The
terminal command connects to its session-scoped persistent shell PTY. Isolated
setup/history probes still use **`--standalone`**.
The server inherits that owner's current credential snapshot through Podman's
`--env NAME` forwarding. Values never enter command arguments, host global
environment, registry metadata, or plaintext service config. Upstream's private,
authenticated stdio lease ties server lifetime to its client; container stop
ends remaining processes. Each new owner gets current values; attached clients
reuse the existing snapshot without refresh/unlock. The host shared
service is never stopped, reconfigured, or exposed inside a workspace.

### Concurrent web, terminal and full TUI clients

```sh
oc-box --project ~/devel/demo
# In another host terminal while the TUI is active:
oc-box web --project ~/devel/demo
oc-box terminal --project ~/devel/demo
```

`web` opens the native browser UI through a host-loopback relay and a native
one-use pairing link. `--no-open` prints the link; `--port N` selects a local
port (default automatic). Keep the foreground web command open; Ctrl-C ends
only that relay. It can also own a normal credential-aware V2 launch when no
TUI is active. A later ordinary `oc-box --project PATH` joins with the full TUI.
`terminal` opens the session's actual shell terminal, also visible in the native
TUI/web terminal panel. It reuses the session's currently controlled running
terminal (or the last running terminal), creating one only if none exists. The
shell starts in the session project and uses the existing server environment.
`Ctrl-]` detaches, retaining shell state for later reattachment; typing `exit`
ends the shell. Connecting takes interactive control and resizes its viewport;
other clients remain connected and can regain control through their terminal
panel. Attached clients use the running service, not a fresh credential snapshot.

Same-user clients obtain a memory-only lease through a private host runtime
Unix socket; no endpoint/password is saved to registry or service config. The
owner retains the lifecycle lock and waits for attached clients before history
sync/container stop. Closing the TUI does not kill an attached web/terminal
client. `oc-box stop` disconnects all; backup/reset/purge require owner exit.
Web traffic travels over Podman exec streams to container loopback without port
publication or LAN exposure. A printed pairing link grants service access;
do not share it. The browser is launched with a sanitized host environment.
`terminal --maintenance` retains the original restricted maintenance shell,
with the lifecycle lock and no config/auth refresh or credential forwarding.

Host history/auth operations also use private servers with the normal host DB
and isolated config/state roots. Native OpenCode saved/OAuth credential refresh
and copying remain independent in every credential mode, including global vault
mode. Existing provider credentials use supported copying into private container
storage, as auth files were for 1.x. The separate
oc-box vault stays on the host. Credential export, refresh, and activation use
supported CLI/API contracts, pipes and memfd; no host plaintext credential files.

### Global vault-first from the first project launch

For a V2 workspace without an explicit credential policy, the default is global
vault-first. Install optional vault support and populate the encrypted host
vault **before the first project launch**:

```sh
# Choose one installation channel:
pipx install 'oc-box[vault]'
# Or: uv tool install 'oc-box[vault]'
# Or GitHub bootstrap with private crypto support:
# curl -fsSL --proto '=https' --tlsv1.2 https://raw.githubusercontent.com/groopcd/oc-box/master/install.sh | sh -s -- --with-vault
oc-box secrets init
oc-box secrets set ANTHROPIC_API_KEY
oc-box secrets set MCP_API_KEY
mkdir -p ~/devel/demo
oc-box --project ~/devel/demo
```

Secret values/passphrases are entered only in masked host-terminal prompts,
never on the command line or in JSON. No per-project registration or frozen
automatic name list is needed: every normal owning start/reset/import/web
unlocks once and delivers **all current stored entries**. Later additions,
removals, and value changes take effect on the next new owner. A configured V2
global vault replaces ambient automatic `_TOKEN`/`_API_KEY` and `pass_env`
forwarding, including legacy `OPENCODE_AUTH_CONTENT` overrides, rather than
supplementing it. Vault env values remain in launcher memory and
private container environments, never host os.environ, argv, registry or service
config. Neither the vault nor the passphrase is mounted into the container.

Without a host vault, V2 keeps the env-file fallback and prints concise setup
guidance. Native OpenCode saved/OAuth credential refresh and copying remain
independent whether the vault exists or not. It never installs crypto, initializes
the vault, edits config, or imports JSON secrets automatically. An initialized
empty vault still unlocks but delivers no entries and suggests
`oc-box secrets set NAME`. A present corrupt vault, wrong passphrase, or missing
crypto support aborts before setup/reset deletion, with no silent env fallback.
Global vault delivery concerns env-variable entries, not structured OAuth
refresh-token records. A vault containing only `BRAVE_API_KEY` must not disable
existing Copilot OAuth. Ordinary supported host OpenCode credential refresh/copy
continues in every mode; no plaintext OpenCode JSON secrets are imported or
rewritten into the vault. Existing copied/container config/auth values remain
independent; changing mode does not revoke them.

### Provider and MCP references, not JSON secrets

Merge only the needed fields into global
`~/.config/opencode/opencode.json(c)` or project `opencode.json(c)`:

```json
{
  "$schema": "https://opencode.ai/config.json",
  "providers": {
    "anthropic": {
      "settings": {
        "apiKey": "{env:ANTHROPIC_API_KEY}"
      }
    }
  },
  "mcp": {
    "servers": {
      "example-tools": {
        "type": "local",
        "command": ["example-mcp-server"],
        "environment": {
          "MCP_API_KEY": "{env:MCP_API_KEY}"
        }
      }
    }
  }
}
```

These are the published V2 `providers.PROVIDER.settings` and
`mcp.servers.NAME.environment` shapes. Replace the placeholder command with an
MCP executable installed in the container. Remote API-key-only servers can use
`mcp.servers.NAME.headers` with `"Authorization": "Bearer {env:MCP_API_KEY}"`
and `"oauth": false`; prefer native OAuth sign-in when available. Do not use
shell `$NAME` expressions or literal keys in JSON. Config reference substitution
uses the service's environment; MCP subprocesses and shell tools inherit the
relevant server/session environment. Non-secret localhost endpoints become
`host.containers.internal`; vault values stay exact. Start a new owner after
changing vault values/names; attach or native config reload does not replace the
service environment. Changes to host config are copied at the next owning
launch, while native V2 reload operates on the existing container copy.

### Intentional workspace overrides and one-off opt-out

Existing explicit `env` or `selected` workspace policies remain overrides. The
accepted credential modes are `env`, `selected`, and `vault`. V2 `vault` mode
uses all global entries dynamically and refuses nonempty per-name lists. After
an existing owner's clients exit, switch an old selection with:

```sh
oc-box credentials --project ~/devel/demo --credential-mode vault --clear-credentials
```

`selected` permits only explicitly named env/vault credentials; `env` retains
legacy suffix/pass_env forwarding and any selected vault supplement. Neither
mode is replaced automatically by global defaults. V1 legacy env-file defaults
are unchanged. Running V1 with saved `vault` mode uses legacy env forwarding
without unlocking all-global entries; explicit old env/selected vault selections
still work as before.

V2-only `--no-vault` skips vault use for one **owning** start/reset/web invocation
(including start with `--import-session`) without saving policy. Global mode
uses legacy env forwarding for that invocation; explicit env/selected modes
skip vault entries while retaining env constraints:

```sh
oc-box start --project ~/devel/demo --no-vault
oc-box reset --project ~/devel/demo --no-vault
oc-box web --project ~/devel/demo --no-vault --no-open
```

The next new owner returns to the saved/default policy. Attached clients reject
`--no-vault` and never refresh/unlock; V1 and info/maintenance/sync/backup/restore/
credential management and other unrelated commands reject the flag too. It
does not remove secrets already persisted in workspace files or copied auth.
For a persistent deliberate legacy override use
`oc-box credentials --project ~/devel/demo --credential-mode env --clear-credentials`.
Only in legacy env mode, names (not values) can be added to host oc-box config:

```json
{
  "pass_env": ["MY_MCP_SETTING", "MY_PROVIDER_SETTING"]
}
```

Use the global vault as the primary V2 workflow; sourcing an env file is only a
one-off compatibility route, not required for global vault delivery.

### Plain host OpenCode 2.x

The oc-box plugin adapters apply only to its copied container config, not your
ordinary host `opencode --standalone` setup. To prepare host legacy plugins, use
the opt-in source utility described in [host plugin migration](host-plugins.md).
It creates an independent V2 profile, keeps the original files untouched and
uses a separate host sandbox port; real host sandbox enforcement needs validation.

Plain `opencode --standalone` is a one-off host tool using normal host
environment/auth; it does **not** access the oc-box vault. There is no oc-box
`--standalone` command. oc-box continues to own its private foreground V2
workspace service. For one-off env-file compatibility on the host:

```sh
. ~/.config/provider-credentials.env
opencode --standalone
```

Upstream also supports shared-service env settings through
`opencode service set env NAME VALUE` and `service.json`. Those put values in
argv and persist plaintext settings; oc-box does not use them for secrets. Use
OpenCode's interactive auth flow for a shared host service, or start a foreground
server from the intended environment yourself. V2's in-memory session-environment
API can supply shell variables but does not replace server-start variables for
provider/MCP config resolution.

### Permissions: container Build default versus native autoapproval

oc-box defaults to no prompts for ordinary tool permission **asks inside
container Build mode only**. The existing final Build-agent wildcard still
overrides copied ordinary global/Build-agent tool permission rules, including
their configured denies, as before. This is not a promise to preserve every
ordinary copied Build/global denial. The new generated V2 helper changes only a
final `ask` decision to `allow` for the resolved actual Build agent and never
overwrites a deny passed to it. Explicit session and mandatory policy denials
remain denied, as do denials from managed rules that apply after the private
overlay. Unknown-agent requests retain native ask/deny outcomes and are never
treated as Build. All non-Build/Plan/child rules and plain host permissions
remain unchanged. V1 behavior is unchanged.

An explicit permission event agent wins. When the event omits the agent, the
helper resolves the current session's agent, then the native configured default
agent; it never assumes the parent's agent. A Build parent therefore does not
give Plan/child requests Build's permission treatment. Session permission arrays
are not overwritten, avoiding inherited Build allowances leaking into Plan or
child requests. Question prompts and MCP elicitation prompts are not tool
permission asks and are not autoaccepted by this helper. A copied plugin can
still enforce its own checks; container permissions do not override host
Podman/kernel limits.

The final private `OPENCODE_CONFIG_CONTENT` overlay reenables only the generated
Build helper against lower-priority project plugin removal entries. This overlay,
not an assumption that an external agent transform runs after all config, is
authoritative for helper activation; managed policy still wins. No `--auto` flag
or client-wide autoaccept setting is added to oc-box clients. The new native
evaluator/import/reload fixture targets these boundaries; it is not live host
acceptance.

For an optional one-off **plain host** launch, the native V2 equivalent of
auto-approving permission asks is:

```sh
opencode --auto --standalone
```

This uses normal host env/auth, not the oc-box vault. `--auto` approves asks but
does not override explicit denials, including configured denies on the plain
host. In an already-running native TUI, **Ctrl+P →
Open settings → Sessions → Permissions → autoaccept** is the equivalent user
choice (`cli.json` `session.permissions`). That preference is a broader client
setting, not a Build-only policy; enabling it can persist in that TUI's global
CLI settings. oc-box does not enable it or modify normal host `cli.json`; it is
not the recommended fix for a container Build-default regression. No new live
permission-prompt/reload pass has been accepted yet. See the published
[native commands](https://opencode.ai/docs/cli/commands) and
[CLI settings](https://opencode.ai/docs/cli/config) references.

## Compatibility map

| Feature | 1.x | 2.x behavior |
| --- | --- | --- |
| Exact runtime | `opencode-ai@VERSION` | `@opencode/cli@VERSION`, decorated version output normalized |
| TUI | Native 1.x | Native 2.x on the workspace-owned private service |
| Env/vault | Legacy env default; explicit selections retained | Global vault-first without an explicit policy; fresh owner snapshot, image leftovers cleared |
| Config/agents/skills | Existing files | Supported V1 fields and V2 files retained, `cli.json` copied |
| Service settings | No shared service | `service*.json` excluded; no remote/hostname/env reactivation |
| Build permissions | Unchanged private V1 hook | Ordinary copied Build/global rules overridden; final Build asks allowed; session/policy denials and Plan/child rules preserved |
| Plugins | V1 SDK/hooks | Native V2 plugins plus bounded adapters for supported copied V1 hooks |
| MCP | Native tools | Native V2 MCP and default Code Mode preserved |
| Formatter | Native | Native |
| LSP | Native language servers | Upstream 2.2.6 does not run LSP/diagnostics; use lint/typecheck/compiler tools |
| History | V1 export/import, part verification | `session export/import`, native transcript verification, distinct namespace |
| Model/new/resume | V1 CLI flags | Session create/switch-model API and launch-local TUI config |
| Backup/reset/restore | Complete private home | Both databases and major selection preserved, internal paths retained |
| Status/doctor/completion | Available | Available without starting OpenCode |
| Terminal/web | Maintenance shell | Actual session shell PTY/full TUI/web coexist; `--maintenance` retains restricted shell |

The generated oc-box plugin is ported. Copied V1 standalone sources from the
autoload directories or explicitly configured paths under the copied config
root get native V2 entrypoints, including `custom-plugins/` and sources without
SDK imports. Native V2 directory entries are preserved. Host originals and V1
behavior remain intact; private source copies use non-autodiscovered modules.
The bridge supports `shell.env`, `tool.execute.before/after`, `tool.definition`,
outbound `experimental.chat.messages.transform`, `chat.headers`, text custom
tools, provider-discovery `config` hooks, and
`dispose`. Shell hooks translate V2 `shell` to V1 `bash`; refusal propagates as
a native tool error. Result conversion retains nontext file attachments and
structured output. Definition callbacks get serializable JSON schemas at startup
and every primary request (including advertised MCP/Code Mode tools); internal
cyclic validation objects are not reported as zero-sized wire schemas. Outbound
message conversion preserves original native media, reasoning and provider
metadata unless a supported text/PDF/tool-input edit changes that specific part.
Message identity, role and order changes are refused. Header hooks get the
selected model variant. The copied Copilot effort workaround's private-header
contract is also applied at V2's native HTTP request boundary, preserving an
existing reasoning field and stripping its private header.
Config edits outside provider discovery, V1 client
DB/history APIs, auth/provider/other chat/event hooks and multi-factory modules require
a native V2 port and are reported rather than silently ignored.

`opencode-sandbox` uses a generated entrypoint invoking its existing container
opt-out (`OPENCODE_DISABLE_SANDBOX=1`). Podman remains the isolation boundary;
no nested Bubblewrap or replacement tools are introduced. Package versions,
options, and explicit removal entries are retained. Configured local sources
are rewritten to their corresponding directory entrypoints. The cache uses
`@opencode/plugin`, retaining custom dependencies and per-runtime cache identity.
Cold V2 setup installs the copied config manifest with npm; warm launches reuse
its lockfiles/modules instead of reinstalling. Supported SDK-only and custom
dependencies are retained; they are never installed into host Python or Node.

Copies of the reported Lemonade discovery/tool, no-nested-sandbox, output-clamp,
Python isolation and tool-definition audit plugins all activated under native
2.2.6 in a disposable fixture. Live Lemonade discovery/tool results, model
capabilities/context, Python isolation variables, output clamping/spill files,
and tool-definition audit reports were exercised against synthetic services.
The packaged sandbox
0.6.0 container opt-out and supported hooks also run in the regular V2 smoke.
Host source files are never modified by this adaptation. The known copied
`output-clamp.ts` implementation receives an exact, private-V2 UTF-8 repair:
threshold checks and head/tail slicing use bytes, preserve whole code points,
and retain the complete spill file. The host and V1 original remain unchanged;
unrecognized plugin implementations are not rewritten. The audit report caveat
is updated in its copied source to describe JSON-schema measurement.

Configured sandbox-uncloak, PDF extraction and Copilot effort plugins now load
and have behavioral tests: historical wrapped input is cleaned only in outbound
model context, real PDF bytes become text, and selected effort reaches a synthetic
Responses endpoint. Unicode cases at 9,000/24,000 bytes are bounded to 8,192 bytes
without splitting characters. Lemonade discovery uses a fixture server; a stopped
real Lemonade server is expected to be unreachable and no real model is loaded.

## History and downgrade strategy

First V2 startup seeds container `oc-box-v2.db` from a consistent SQLite snapshot
of retained `opencode.db`. Upstream migrates the snapshot; oc-box waits for its
migration status before using history. Original V1 bytes remain intact.
`sessions.json` and `sessions-v2.json` keep major-specific resume/archive state.
V2 archives use a distinct namespace so the same source ID cannot replace a V1
archive. Host imports never overwrite original host conversations.

Switching the host executable to 1.x reinstalls its exact container runtime and
V1 overlay, selecting retained V1 history. Returning to 2.x resumes existing V2
progress rather than reseeding. The histories diverge after transition; there
is no bidirectional live merge. Keep user config V1-compatible for downgrade;
native V2-only user config and external plugin APIs are upstream boundaries.

V2 transcript import expects native V2 messages. To move a V1 conversation into
a V2 workspace, let upstream migrate its source DB, then export/import with the
matching V2 CLI. oc-box does not hand-translate tool history or write native DB
rows. Full/portable backup contains both private databases and remembers the
archived major; normal launch subsequently matches the current host release.
Backups retain workspace credential modes/policies but exclude the global host
vault and outside env files. Protect the encrypted host vault separately. A
restored default/global V2 workspace uses the restoring host's current vault,
not the original host's vault or old privileges. Full backups may contain values
already persisted in workspace config/auth/project/home/OS; delivered credentials
remain readable by the agent/tools and can be deliberately written to disk.

## Verification

```sh
python3 -B tests/smoke_mainline.py
python3 -B tests/smoke_v2.py --opencode /path/to/opencode-v2
```

V2 testing uses temporary config/databases and offline synthetic model/MCP
services. It checks tools/formatting, MCP Code Mode execution, env substitution,
Build/Plan behavior, large native history transfer, duplicate protection, host
sync, credential rotation, and real upstream V1-to-V2 snapshot migration with
unchanged V1 source bytes. It does not test real provider inference or Podman.
It also checks a live foreground owner, same-user client leases, actual session
shell detach/replay/retained environment while a browser relay is open, one-use
browser pairing, cookies, shared process/session identity and event streaming. Binary upgrade
traffic and lifecycle lock/close ordering have separate offline fixtures.

Accepted host testing for the 1.5 release covers:

- 1.x env/MCP operation and 2.2.6 MCP access, cold/warm startup, native TUI,
  web and persistent session-shell coexistence, detach/reattach, stop/resume,
  updated plugin behavior and host history sync.
- Selected synthetic vault provider/shell/MCP delivery, rotation and attach
  without unlock, exercised by the real rootless-Podman fixture.
- Full/portable restore with fresh IDs, stopped containers, exact private-home
  database bytes before reopen, retained session/internal paths, source
  preservation and expected OS-state differences. Reset preserved project/home/
  history and rebuilt the catalog OS; wrong-password reset preserved resources.
- Native V1 downgrade preserving exact original messages/parts and leaving V2
  database bytes unchanged, followed by return to V2 with all existing native
  messages intact and new synthetic provider/shell/MCP execution. V2 left the
  retained V1 database bytes unchanged.

These accepted selected-vault host probes do **not** cover the new global vault
default, first-launch delivery, dynamic addition/removal or `--no-vault`.
Global-vault unit/native offline coverage must be reported separately; no live
new global-vault Podman lifecycle pass has been accepted yet.

The initial full-image load hit a quota in system temporary storage. A fresh
disk-backed run completed restore/reset, then a targeted retry completed the
downgrade/V2 return after correcting an overly strict active-V1 byte comparison.
The targeted retry did not repeat the earlier restore/reset checks. Native V1
updates project/access metadata on normal reads; exact transcript comparison
checks history once V1 itself reopens the database. Inactive-major database bytes
and unopened backup/restore transport remain separately verified.

The provider/MCP fixture is synthetic, with scripted TUI/password input and real
selection/decryption, forwarding, locks, services, tools and rootless storage.
Interactive host testing covers the native client UI separately. These checks
do not claim restored real-project model inference or universal filesystem/OS
certification. Automated offline fixtures do not replace host Podman validation.

### Isolated global-vault and Build approval host validation

Before using a new credential-default build with important work, run the
V2-only fixture from a regular **host terminal**, outside oc-box and other
shell sandboxes, without sudo. It runs the source checkout directly; it does
not install/upgrade your launcher, replace the host OpenCode binary, or use your
normal provider accounts, encrypted vault, configuration or conversation DB.

```sh
uv run --isolated --no-project --with cryptography python -I -B tools/test_global_vault_podman.py \
  --opencode-v2 "$(command -v opencode)" \
  --distro tumbleweed --work-dir /var/tmp --temp-dir /var/tmp
```

The native executable must be unmodified OpenCode 2.2.6. No separate V1 binary
is needed for this focused fixture. `--work-dir` and `--temp-dir` must be
existing disk-backed parent directories; the fixture creates fresh private
children. Choose another suitable filesystem if `/var/tmp` has insufficient
space/quota. Allow space for two cold environments, the captured OS image,
encrypted staging and a full restore; a full backup can be several GiB. Neither
option relocates retained Podman storage or encrypted evidence.

The preflight checks the native engine's actual rootless/local status, overlay
driver, graphroot/runroot, named-volume location and temporary-image location.
Podman 6.1.4 is explicitly supported although its info response no longer
includes `store.configFile`: the validated private selector and configuration
remain mandatory, and actual paths are checked in every subprocess environment.
Other unverified omissions fail closed; do not bypass that check to run a test.

The fixture creates an isolated Podman graphroot/runroot and verifies the
effective store before managed resources. It uses synthetic encrypted vault
entries, private host history and disposable project directories. It exercises
first-project conversation import with global entries, a second project's
dynamic entries/rotation, an active owner's unchanged startup values, scoped
Build approvals and reload, a one-off `--no-vault` compatibility launch, and
encrypted full backup/restore with fresh IDs, retained internal paths,
database bytes before reopen and native resumed history. TUI interaction and
vault prompt input are scripted; OpenCode, credential decryption, tool execution
and rootless lifecycle/storage are real. This is not live interactive TUI or
comprehensive sandbox certification.

Success writes a private `result.json` and prints the retained fixture path.
Failure also retains its resources and evidence; do not move the graphroot,
overwrite an archive, or delete markers to retry. Review the report before
recording a host acceptance result. Cleanup is explicit: use the exact printed
`tools/test_global_vault_podman.py --cleanup` command. It verifies the isolated
store and owned resource labels, never runs global prune, and leaves synthetic
projects, encrypted archives and their fixture-only identity for inspection.
The private Podman selector records its canonical base Python interpreter and
native engine path. Cleanup validates that recorded identity instead of the
current virtual environment; those recorded binaries must remain available.
It never rewrites an unrecognized selector to bypass validation.
New first-owning-launch failures also retain `first-launch-failure.json`, with
fixed substeps/classes/exit codes and the primary failure separated from
launcher cleanup errors. This is failure evidence, not acceptance. A reporting
failure cannot replace the primary diagnostic or overwrite an earlier report.
Native CLI API replies with HTTP 204 carry no JSON body: permission replies,
session deletion and location reload now return `None` through the workspace
transport, matching the private HTTP bridge. Failed commands and malformed
nonempty JSON still fail. The native helper regression checks this CLI path,
not only HTTP; it is not a new live Podman acceptance result. A propagated
callback error after a completed stop is not recorded as a new shutdown failure.
The first-owner host-clean check runs after the fixture's synthetic ambient
control injection is removed. Vault/cache retention is also checked before that
environment restoration, so restoration cannot conceal an accidentally inserted
vault entry; deliberate ambient controls are never mistaken for vault delivery.

For an unsuccessful owning launch, inspect retained checkpoints without
starting/executing a container, unlocking the vault, syncing or replaying:

```sh
uv run --isolated --no-project --with cryptography python -I -B tools/test_global_vault_podman.py \
  --diagnose /var/tmp/oc-box-gv-host-EXAMPLE
```

Use the exact retained path printed by your run; do not combine this with
cleanup, executable or work/temp placement options. A file-only checkpoint first
compares the fixture's cached Podman boot marker with the current kernel boot
ID, reporting booleans only. A stale, absent, empty, invalid or unreadable marker
refuses engine/resource inspection; no Podman command is run. On a matching boot
the diagnostic validates the private store and labels, then returns only
readiness/state/exit-code and
import/session checkpoints. Container error text is reduced to fixed categories,
never printed alongside env/config values. It cannot recover an exception that
the original run discarded, and inspection does not establish acceptance.
If store validation fails, it exits nonzero with `status: diagnostic-failed`
and fixed-category `engine_probes` instead of discarding the engine failure.
These compare the recorded selector/native `--version` and private `info`
queries in normal, maintenance and backup environments. Direct native queries
retain both validated private config selectors; they never query the operator's
normal store. Output includes only exit codes, output-presence flags, fixed
categories, validated numeric versions and store-verification booleans. Raw
stdout/stderr stays in anonymous memory. No resources are inspected after the
initial refusal, even if a comparison succeeds; no launch, unlock, reset,
cleanup, selector/config rewrite or acceptance follows from these probes.
Podman `info` is informational, but its runtime initialization can refresh state
and write metadata; it is not a guarantee of database-byte immutability. The
file-only boot guard avoids implicit refresh on an old/uninitialized runtime.

**Disk-backed fixture runtime state is limited to its original boot.** Podman
6.1.4 rejects a cached/current boot-ID mismatch. This fixture now identifies it
before engine calls rather than following Podman's directory-deletion advice.
`--retry-source` and `--cleanup` also refuse it; never delete/rewrite the marker,
move either store root, reset the engine or edit the stored paths to resume.
Retain that fixture unchanged. A new run under `--work-dir` creates a separate
fresh synthetic fixture with a new history/vault/store, not a replay or recovery
of the old one; only its own complete report can establish a new host result.

If the retained source is on the same verified boot, stopped, baseline-ready and
already has its verified seed recorded, a narrowly scoped owning retry can
capture the failing substep:

```sh
uv run --isolated --no-project --with cryptography python -I -B tools/test_global_vault_podman.py \
  --retry-source /var/tmp/oc-box-gv-host-EXAMPLE
```

This refuses fixtures with backup/restore/result evidence or an unrecorded
selection, wrong project or non-root session. It resumes the existing synthetic source, never reimports,
resets or purges it. Each attempt creates a new private companion vault/config
and report, leaving the original encrypted vault, seed and configuration
evidence unchanged. The lost random fixture passphrase is not recovered or
written to disk. Only owning-source readiness, retained history and native
tool/permission checks are retried; success does not repeat or establish the
original first launch, new-project/opt-out/full-backup/restore stages.
Failure reports fixed substeps/error classes/exit codes rather than raw private
stderr, env values, argv or transcript contents. Keep the attempt report and
original fixture; do not use the whole new-fixture command as a retry.
If another recorded synthetic root became the normal current selection, the
retry validates that client's session/project metadata, then targets the
verified imported seed explicitly for its native checks. Both IDs are reported
and the other session's messages are checked unchanged. It never hand-edits
the selection file; normal launcher history refresh may update its last-used ID.

**No live result for this fixture has been accepted yet.** Unit and native
offline tests do not substitute for executing it on a host. Keep the important
project untouched until the desired validation passes.

### Repeatable host validation

Versions use **1.5.N**, preserving the original commit-count patch baseline and
upgrade ordering. Repeat relevant host validation before lifecycle/credential
releases, using only isolated disposable resources.

#### Automated disposable host fixture

Run `tools/test_v2_podman.py` from the repository root in your regular Linux host
terminal. It cannot run from this development container: host Podman resources
are not exposed, and nested Podman would not test the host lifecycle. The fixture
creates private temporary Podman graphroot/runroot, registry, config, host DBs
and encrypted synthetic vault. It never uses the operator's vault, env-file/provider values, projects,
models, or shared OpenCode service. Use separate unmodified 1.19.5 and 2.2.6
executables to avoid replacing your installed OpenCode. For example:

```sh
git pull --ff-only
fixture_tools=$(mktemp -d /tmp/oc-box-v2-tools-XXXXXXXX)
npm install --prefix "$fixture_tools/v1" --no-audit --no-fund opencode-ai@1.19.5
npm install --prefix "$fixture_tools/v2" --no-audit --no-fund @opencode/cli@2.2.6
uv run --no-project --with cryptography python -B tools/test_v2_podman.py \
  --opencode-v1 "$fixture_tools/v1/node_modules/.bin/opencode" \
  --opencode-v2 "$fixture_tools/v2/node_modules/.bin/opencode"
```

By default the fixture places its image store and temporary files under one
private system-temp directory. For a large full image on a disk-backed test
filesystem, pass an existing parent using `--work-dir /var/tmp`. This creates a
new random private fixture there; it does not move or reuse the old image store.
Set `--temp-dir /var/tmp` to place Podman's temporary image files and Python's
encrypted-component staging in a separate private directory under that parent.
Both options accept any existing parent suitable for the host filesystem/quota.
For example, use the same native executables above:

```sh
uv run --no-project --with cryptography python -B tools/test_v2_podman.py \
  --opencode-v1 "$fixture_tools/v1/node_modules/.bin/opencode" \
  --opencode-v2 "$fixture_tools/v2/node_modules/.bin/opencode" \
  --work-dir /var/tmp --temp-dir /var/tmp
```

Requires host rootless Podman prerequisites, age/age-keygen, Git, npm and uv for
these setup commands; see `oc-box doctor`. The default test distro is Tumbleweed
(`--distro NAME` selects another catalog distro). Expect image/package downloads,
separate exact-version runtime installs and full encrypted image transfers;
the run can take several minutes and needs room for three temporary workspaces
plus encrypted backups. Its isolated Podman storage downloads test images even
if your regular host storage has them cached. It invokes production commands
against fixture-owned resources only. A host-loopback synthetic provider and container-local MCP verify
native tool execution and selected-vault credentials, without real inference.

Checks include credential rotation, attach without unlock, wrong-password reset
preserving the container, V1 snapshot migration, full/portable byte-exact restores
and native V2 launches, reset with retained home/history and OS rebuild, V1
downgrade and V2 return without lost progress. The TUI and masked password input
are scripted fixture actions; selection, decryption, forwarding, native servers,
tools, resource locks and rootless storage are real production paths. Your
interactive TUI/web/shell testing covers the separately accepted UI behavior.

Send the final PASS lines and the printed `result.json` report. On success or
failure the fixture is retained for diagnosis. Its private keys/vault/archives
are synthetic test data; do not publish the fixture directory. The printed
cleanup command removes only fixture-owned Podman resources, retaining files:

```sh
python3 tools/test_v2_podman.py --cleanup /tmp/oc-box-live-v2-EXAMPLE
```

The fixture has offline unit tests for host refusal, environment isolation,
host-executable routing, credential checks and cleanup boundaries. Native V2 CI
also exercises its exact provider/MCP config and rotated environment without
Podman. Only a successful run on the host closes the live acceptance gate.

The fixture supplies explicit stdin EOF to noninteractive native V2 `run`
commands. OpenCode 2.2.6 reads Podman exec's open FIFO until EOF even when a
prompt is supplied in argv; without that EOF it waits before contacting the
provider. CI reproduces this wait and verifies the fix against a native private
server. A short host-loopback provider probe now fails before inference when
pasta/firewall connectivity is unavailable.

For a failed **source-only run before backups begin**, retry its retained
container/runtime cache without repeating setup downloads:

```sh
git pull --ff-only
uv run --no-project --with cryptography python -B tools/test_v2_podman.py \
  --resume /tmp/oc-box-live-v2-EXAMPLE
```

Resume validates the private fixture directory, resource identity, isolated
Podman storage and stopped state, then generates new synthetic credentials and
a new fixture provider port. It reuses the recorded native executables and V1
seed history. It refuses fixtures with backup/restore evidence or missing
executables; those need a new run after fixture-only cleanup. Resume does not
accept replacement executable paths.

Before container startup, both separate test executables are probed directly
with `--version` in isolated host roots and checked against 1.19.5/2.2.6. Retry
refreshes only the fixture-generated selector, checks both routed versions, and
uses that selector's absolute path for host history/version operations instead
of relying on a PATH-selected `opencode`. A mismatch reports the expected and
detected version plus its executable path; it does not reinstall or modify those
binaries automatically. If the retained executable itself has changed, inspect
the reported path and prepare a new fixture with separate exact-version installs.

If the source fixture reaches full backup creation but fails during image load,
preserve its encrypted archive and retry only that restore:

```sh
git pull --ff-only
uv run --no-project --with cryptography python -B tools/test_v2_podman.py \
  --retry-full-restore /tmp/oc-box-live-v2-EXAMPLE
```

This reports available bytes/inodes for fixture storage, Podman's temporary
image directory (`TMPDIR`, normally `/var/tmp` outside this fixture) and encrypted
staging. It authenticates the entire image component into a discard stream,
then retries the unchanged backup
at the still-nonexistent `restored-full` destination. It refuses existing restore
or completed-test evidence. Full restore success writes `full-restore-result.json`
and records the fresh ID for cleanup; this limited retry is not proof of native
restored inference, portable restore, reset or downgrade. Pipeline failures show
producer/consumer exit codes and fixed diagnostic categories, never raw tool
stderr or decrypted image bytes.

To retry only the old backup with temporary files on another filesystem:

```sh
uv run --no-project --with cryptography python -B tools/test_v2_podman.py \
  --retry-full-restore /tmp/oc-box-live-v2-EXAMPLE --temp-dir /var/tmp
```

The fixture forwards the private temporary directory as `TMPDIR` to Podman and
Python staging, and restores the calling process's original tempfile default
afterward. Temporary override directories are cleaned on exit; retained source
resources, encrypted backup and isolated image store are unchanged. An override
changes temporary placement only, not the existing store's graphroot/runroot.
If image storage itself is quota-limited, run a new full fixture with `--work-dir`
on a filesystem with adequate user/project quota rather than copying live Podman
storage or overwriting the old backup. Keep the original backup until accepted.
`Disk quota exceeded` is separate from filesystem-wide free bytes; zero inode
counts can mean the filesystem does not report inode capacity. Check the actual
user/project limits before interpreting the available-space lines as sufficient.

For a fixture that has completed full/portable restores and reset but stopped
at downgrade, complete only the version transitions while preserving its backups:

```sh
git pull --ff-only
uv run --no-project --with cryptography python -B tools/test_v2_podman.py \
  --retry-downgrade /var/tmp/oc-box-live-v2-EXAMPLE --temp-dir /var/tmp
```

This requires exactly the recorded source, full restore and portable restore,
all stopped, plus both encrypted backups and the original private V1 import
transcript. It validates ownership, isolated graphroot/runroot and restore origins,
authenticates both archive manifests, and snapshots current V2 transcript progress
before reopening V1. It compares the complete V1 message/part transcript against
the original import and verifies V1 has not written any V2 database bytes. After
returning to V2 it verifies all retained message contents/order and executes the
synthetic provider/shell/MCP check. It does not recreate restores or rewrite the
encrypted backups, and refuses existing completion evidence. The result is
`downgrade-result.json`; earlier restore/reset results remain separate host test
evidence, not checks claimed to have run again by this targeted command.

Native OpenCode 1.19.5 updates its `project` access metadata during ordinary
`debug agent`, `session list` and `export`. Database-byte equality is therefore
required only while the *other* major is running; after V1 itself reopens its
database, exact retained transcript comparison is the history-preservation check.
Private-home backup/restore transport remains byte-exact before either runtime
reopens restored files. This distinction is exercised against unmodified V1/V2
in CI and does not permit changed/lost messages or overwritten session identity.

#### Manual alternative

Use only disposable projects, credential selections and backups:

- **Selected vault credentials:** launch V2 using a selected synthetic vault
  value and verify it inside that workspace; change the value and relaunch to
  verify refresh. Attached web/TUI/shell clients must retain the owning launch's
  snapshot and must not unlock again.
- **New global vault acceptance (pending):** initialize/populate an isolated
  synthetic vault before the first launch, verify all entries reach provider,
  MCP and shell, add/remove entries and start a new owner to check dynamic
  selection. Verify empty/absent/corrupt vault handling, wrong-password reset
  preservation, intentional env/selected overrides, V1 return, and one-owning-
  invocation `--no-vault` without saved-policy changes. Attached clients must
  retain their snapshot and reject `--no-vault`. These require new host evidence;
  offline checks and the earlier selected-vault pass do not close this gate.
- **V2 backup/restore:** create both full and portable backups after the owner
  and attached clients exit, restore to fresh destinations, then launch and
  verify sessions, internal paths, private-home data and expected OS differences.
  The older `tools/test_backup_podman.py` fixture verifies backup transport and
  rootless ownership with synthetic database bytes; it does not by itself verify
  restored native V2 inference or service lifecycle.
- **Reset/downgrade:** reset a disposable V2 workspace and verify retained
  project/private-home/session data. Select a separate host 1.19.5 executable and
  launch to verify retained V1 history, then select 2.2.6 again and verify V2
  progress was not reseeded or lost. Keep test configuration V1-compatible.

Record host evidence, regenerate changed manuals, and commit release documentation
after checks. Continuous CI runs native V1/V2 integration on branches, pull
requests and master, alongside regression/package checks and the seven-distro
matrix. Fast-forward the tested
commit into master, publish its matching commit-derived tag, and verify the
GitHub/PyPI wheel/sdist source identity, installation and SHA256 checksums.

## Upstream references

- [V1 migration](https://opencode.ai/docs/migrate-v1)
- [CLI and standalone mode](https://opencode.ai/docs/cli)
- [Config/environment](https://opencode.ai/docs/config)
- [Providers](https://opencode.ai/docs/providers)
- [MCP servers/environment](https://opencode.ai/docs/mcp-servers)
- [Plugin migration](https://opencode.ai/docs/build/plugins/migrate-v1/)
- [Service troubleshooting](https://opencode.ai/docs/troubleshooting)
