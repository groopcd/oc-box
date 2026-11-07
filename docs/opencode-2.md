# OpenCode 1.x and 2.x compatibility

Development branch: `feature/opencode-2-compat`. Released oc-box 1.0.7 supports
OpenCode 1.x. Validation on this branch uses unmodified 1.19.5 and 2.2.6.

## Environment and service plan

OpenCode 2.x still supports environment variables and `{env:NAME}` substitution.
Providers, configuration, plugins, and MCP servers run in the service: connecting
a terminal with a different environment does not replace a running service's
environment.

oc-box uses supported **`--standalone` private-server mode** in each workspace.
The server inherits that launch's env-file/vault selections through Podman's
`--env NAME` forwarding. Values never enter command arguments, host global
environment, registry metadata, or plaintext service config. Upstream's private,
authenticated stdio lease ties server lifetime to its client; container stop
ends remaining processes. Each launch gets current values. The host shared
service is never stopped, reconfigured, or exposed inside a workspace.

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
| TUI | Native 1.x | Native 2.x with `--standalone` |
| Env/vault | Per-exec forwarding | Fresh server startup environment, image leftovers cleared |
| Config/agents/skills | Existing files | Supported V1 fields and V2 files retained, `cli.json` copied |
| Service settings | No shared service | `service*.json` excluded; no remote/hostname/env reactivation |
| Build permissions | Private V1 hook | Native V2 agent/permission hooks; Plan/subagent rules preserved |
| Plugins | V1 SDK/hooks | V2 SDK and directory entrypoint; external V1 plugins need porting |
| MCP | Native tools | Native V2 MCP and default Code Mode preserved |
| Formatter | Native | Native |
| LSP | Native language servers | Upstream 2.2.6 does not run LSP/diagnostics; use lint/typecheck/compiler tools |
| History | V1 export/import, part verification | `session export/import`, native transcript verification, distinct namespace |
| Model/new/resume | V1 CLI flags | Session create/switch-model API and launch-local TUI config |
| Backup/reset/restore | Complete private home | Both databases and major selection preserved, internal paths retained |
| Status/doctor/completion | Available | Available without starting OpenCode |
| Maintenance terminal | No auth/config refresh | Same restricted semantics, no service/credential forwarding |

The generated oc-box plugin is ported. User plugins are preserved and failed
V2 activation gets a targeted message; old implementations cannot be translated
automatically. Install their V2-compatible release or use upstream's plugin
migration guide. Configured local V2 plugins must be directories. The cache uses
`@opencode/plugin`, retaining custom dependencies and per-runtime cache identity.
Cold V2 setup installs the copied config manifest with npm; warm launches reuse
its lockfiles/modules instead of reinstalling. Supported SDK-only and custom
dependencies are retained; they are never installed into host Python or Node.

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

Before merge/release, accept host-terminal testing in a disposable workspace:
fresh/warm V2 TUI, env-file and selected-vault provider/MCP access, stop/resume,
import/sync, full/portable backup restore, reset and V1 downgrade. This development
container cannot validate host Podman. Keep the branch unmerged until accepted.

## Upstream references

- [V1 migration](https://opencode.ai/v2/docs/migrate-v1)
- [CLI and standalone mode](https://opencode.ai/v2/docs/cli)
- [Config/environment](https://opencode.ai/v2/docs/config)
- [Plugin migration](https://opencode.ai/v2/docs/build/plugins/migrate-v1/)
- [Service troubleshooting](https://opencode.ai/v2/docs/troubleshooting)
