# OpenCode 1.x and 2.x compatibility

Development branch: `feature/opencode-2-compat`, pending the remaining host
lifecycle checks before merge/publication. The compatibility code supports
unmodified OpenCode 1.19.5 and 2.2.6. Installing a previously published PyPI or
GitHub release does not install this unmerged work.

## Environment and service plan

OpenCode 2.x still supports environment variables and `{env:NAME}` substitution.
Providers, configuration, plugins, and MCP servers run in the service: connecting
a terminal with a different environment does not replace a running service's
environment.

oc-box owns a private authenticated stdio server in each active workspace.
The full TUI and web clients use native APIs against that same service. The
terminal command connects to its session-scoped persistent shell PTY. Isolated
setup/history probes still use **`--standalone`**.
The server inherits that launch's env-file/vault selections through Podman's
`--env NAME` forwarding. Values never enter command arguments, host global
environment, registry metadata, or plaintext service config. Upstream's private,
authenticated stdio lease ties server lifetime to its client; container stop
ends remaining processes. Each launch gets current values. The host shared
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
and isolated config/state roots. Existing OpenCode provider credentials may be
copied into private container storage, as auth files were for 1.x. The separate
oc-box vault stays on the host. Credential export, refresh, and activation use
supported CLI/API contracts, pipes and memfd; no host plaintext credential files.

### Keep using an env file

From a host terminal, source your existing file and launch the branch:

```sh
. ~/.config/provider-credentials.env
python3 oc_box.py --project ~/devel/demo
```

Automatic `_TOKEN`/`_API_KEY` forwarding, `pass_env`, and workspace selections
retain their semantics. Add custom variable names to host oc-box config:

```json
{
  "pass_env": ["MY_MCP_SETTING", "MY_PROVIDER_SETTING"]
}
```

Provider `{env:NAME}` references and native V2 MCP environment references resolve
in the new server. MCP subprocesses and shell tools inherit the relevant
server/session environment. Non-secret localhost endpoints become
`host.containers.internal`; vault values stay exact. Restart the oc-box client
after changing values or config so its private server picks up the new snapshot.

### Plain host OpenCode 2.x

For a one-off host launch with the current shell's environment:

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

## Compatibility map

| Feature | 1.x | 2.x branch behavior |
| --- | --- | --- |
| Exact runtime | `opencode-ai@VERSION` | `@opencode/cli@VERSION`, decorated version output normalized |
| TUI | Native 1.x | Native 2.x on the workspace-owned private service |
| Env/vault | Per-exec forwarding | Fresh server startup environment, image leftovers cleared |
| Config/agents/skills | Existing files | Supported V1 fields and V2 files retained, `cli.json` copied |
| Service settings | No shared service | `service*.json` excluded; no remote/hostname/env reactivation |
| Build permissions | Private V1 hook | Native V2 agent/permission hooks; Plan/subagent rules preserved |
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

Before merge/release, accept host-terminal testing in a disposable workspace:
fresh/warm V2 TUI, env-file and selected-vault provider/MCP access, stop/resume,
import/sync, full/portable backup restore, reset and V1 downgrade. This development
container cannot validate host Podman. Keep the branch unmerged until accepted.
Accepted host testing confirms 1.x env/MCP operation and 2.2.6 MCP access,
cold/warm startup, native TUI, web and persistent session-shell coexistence,
detach/reattach, stop/resume, updated plugin behavior and host history sync.
Selected-vault credentials, V2 full/portable backup and restore, reset and V1
downgrade host validation are still pending. Automated fixtures verify those
data/credential contracts but do not replace host rootless-Podman validation.

### Remaining release checks

The next published version uses **1.5.N**, preserving the original commit-count
patch baseline and upgrade ordering. This branch is prepared for that series;
publication waits for the host results below.

#### Automated disposable host fixture

Run `tools/test_v2_podman.py` from the repository root in your regular Linux host
terminal. It cannot run from this development container: host Podman resources
are not exposed, and nested Podman would not test the host lifecycle. The fixture
creates a private temporary registry, config, host DBs and encrypted synthetic
vault. It never uses the operator's vault, env-file/provider values, projects,
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

Requires host rootless Podman prerequisites, age/age-keygen, Git, npm and uv for
these setup commands; see `oc-box doctor`. The default test distro is Tumbleweed
(`--distro NAME` selects another catalog distro). Expect image/package downloads,
separate exact-version runtime installs and full encrypted image transfers;
the run can take several minutes and needs room for three temporary workspaces
plus encrypted backups. It invokes production commands against fixture-owned
resources only. A host-loopback synthetic provider and container-local MCP verify
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

#### Manual alternative

Use only disposable projects, credential selections and backups:

- **Selected vault credentials:** launch V2 using a selected synthetic vault
  value and verify it inside that workspace; change the value and relaunch to
  verify refresh. Attached web/TUI/shell clients must retain the owning launch's
  snapshot and must not unlock again.
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

After accepted results, remove the development-release notice, record the host
evidence, regenerate manuals, and commit the release documentation. Continuous
CI runs native V1/V2 integration on branches, pull requests and master, alongside
regression/package checks and the seven-distro matrix. Fast-forward the tested
commit into master, publish its matching commit-derived tag, and verify the
GitHub/PyPI wheel/sdist source identity, installation and SHA256 checksums.

## Upstream references

- [V1 migration](https://opencode.ai/v2/docs/migrate-v1)
- [CLI and standalone mode](https://opencode.ai/v2/docs/cli)
- [Config/environment](https://opencode.ai/v2/docs/config)
- [Plugin migration](https://opencode.ai/v2/docs/build/plugins/migrate-v1/)
- [Service troubleshooting](https://opencode.ai/v2/docs/troubleshooting)
