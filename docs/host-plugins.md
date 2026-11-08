# Plain host OpenCode 2.x: migrate legacy plugins safely

The adapters inside oc-box do not edit your host plugins. Running
`opencode --standalone` outside oc-box therefore still loads your original V1
implementations, which V2 cannot execute.

`tools/migrate_host_plugins.py` creates an **independent host V2 configuration
profile**. It is a source-checkout utility, not an oc-box launcher command. It
does not edit the original config, replace installed OpenCode, change a database,
unlock credentials, or start a service/package manager. The default is a names-only
dry run. The `--apply` operation creates a new private profile and an explicit
launcher. Existing output directories are never replaced.

## Prepare on your host

Run from a source checkout containing the utility, in your regular host terminal:

```sh
python3 tools/migrate_host_plugins.py
```

Review the plugin names and sandbox plan, then create a profile at a new path:

```sh
python3 tools/migrate_host_plugins.py --apply \
  --output "$HOME/.config/opencode-host-v2"
npm install --prefix "$HOME/.config/opencode-host-v2/opencode" \
  --no-audit --no-fund
```

The profile installs its own Node dependencies only. It is tested with unmodified
OpenCode 2.2.6 and its matching plugin SDK. To read another original profile use
`--config /path/to/config/opencode`. Unsupported event hooks, unsafe symlinked
sources and configured standalone files outside the copied root require manual
review instead of a silently incomplete migration.

Source your trusted normal env file and launch:

```bash
( set -a; source /path/to/provider-credentials.env && set +a && \
  "$HOME/.config/opencode-host-v2/run-opencode" )
```

The explicit launcher selects the independent `XDG_CONFIG_HOME` and starts the
installed V2 executable with `--standalone`. Provider secrets stay in inherited
environment, not argv or the generated launcher. An existing `OPENCODE_CONFIG`
override is refused: unset it before launch or review its settings separately
to avoid loading the original incompatible plugins again. Normal host session
data/auth remain in their usual storage; configuration changes do not migrate
or isolate those databases. Native OpenCode may itself perform its supported
V1 history migration on first V2 use, so back up host session data beforehand.

## What is retained

- Copies of supported local V1 plugins from `plugins/`, `plugin/` and explicitly
  configured local sources such as `custom-plugins/`, in native V2 directory
  entrypoints. The bounded tested bridge supports shell environment, tool
  execution/output/definition, custom tools, provider discovery, outbound context
  and headers. Unsupported V1 API assumptions still require native ports.
- Unrelated config values, agents, commands, skills, supporting source files,
  plugin options and native V2 directory plugins. Config JSON comments are not
  retained in the *copy*; original JSON/JSONC is untouched.
- Host endpoints remain host endpoints: `localhost` is **not** rewritten to
  `host.containers.internal`. The V2 profile does **not** add Build allow-all
  permissions or disable integrations.
- Known copied output-clamp fixes use whole-code-point UTF-8 byte limits, and
  tool audit callbacks receive request JSON schemas, as inside oc-box.
- Other host XDG directories, including `opencode-sandbox`, are linked to their
  original locations; this keeps the existing sandbox policy authoritative.
  This is a config snapshot: regenerate a new profile after changing original
  plugin sources/settings. Do not overwrite an existing profile to refresh it.

Existing `node_modules`, auth files, service registrations, `.env`, `.npmrc`, and
oc-box-generated Build/containment overlays are not copied. No config values are
printed. Existing literal credentials in your original config remain in its
private copied settings; prefer `{env:NAME}` and keep both profiles private.

## Host sandboxing is NOT the container opt-out

The `opencode-sandbox` 0.6-style package uses a V1 event/history API not suitable
for plain V2. The utility supplies a dedicated **native V2 shell sandbox port**
using the maintained `@anthropic-ai/sandbox-runtime@0.0.76` engine and the existing
policy files. It does not load oc-box's opt-out adapter or set
`OPENCODE_DISABLE_SANDBOX=1` on the host.

The port defaults to fail-closed shell execution if initialization, policy parsing,
or command wrapping fails. An explicit `mode: "permissive"` retains the operator's
choice to allow unsandboxed commands, with warnings. `disabled: true` and an
operator-provided disable environment variable remain explicit opt-outs. Tool
hooks clean resources on completion/errors and unload without V1 history writes.
Like the original plugin, this is a **shell-tool** boundary, not a claim that the
OpenCode server, all native file tools, MCPs, plugins, or interactive session PTYs
are sandboxed. oc-box remains the option for containing all those integrations.

### Required host test

The generated profile and local hooks pass native V2 offline tests; sandbox policy,
refusal and cleanup pass mocked-runtime tests. **Real host sandbox enforcement
is not validated inside this development container.** Before trusting it, check:

1. `/plugins` reports the migrated local plugins and `oc-box.host-sandbox` active.
2. Run a harmless shell tool in a disposable test project; it should execute.
3. Ask the shell tool to write a synthetic path *outside* your allow-write paths;
   the write must fail and the file must not appear. Do not use real private files.
4. Request a synthetic/non-sensitive network endpoint outside your domain allowlist;
   it must be denied. Test an allowed endpoint separately.
5. Reload with `/reload`; verify another harmless shell call succeeds and unwanted
   proxy/helper processes do not accumulate. Keep commands plain—do not hand-wrap
   sandbox commands.
6. Exercise the individual plugin behavior (Python isolation, output clamp,
   definition audit, PDF/context/header hooks). A stopped Lemonade server is an
   expected discovery result; this migration never loads models automatically.

If sandbox initialization cannot run, enforce mode blocks shell; inspect missing
host prerequisites using the sandbox runtime's normal diagnostics. Do not disable
the policy to make a test pass. Send plugin IDs/status and redacted errors, never
env files, credentials, or private request content.

## Rollback

Exit the generated profile and launch ordinary `opencode` with your original
configuration (or the V1 executable if reverting to V1). No original plugin/config
was changed. This reverts the *configuration*, not any native database migration
or work performed by OpenCode. Keep the generated profile for diagnosis; no
recursive cleanup is necessary.

## Developer checks

```sh
python3 -B -m unittest discover -s tests -p test_host_plugin_migration.py -v
python3 -B tests/smoke_host_plugins.py --opencode /path/to/opencode-v2
```

The second command uses a synthetic provider/MCP and a mocked runtime in private
temporary config/data; it does not certify rootless or host sandbox isolation.
