# Plain host OpenCode 2.x: native plugin migration and audit

## Recommended: native V2 in your normal host configuration

If you use only OpenCode 2.x, `tools/port_host_plugins.py` audits the known personal
plugins and replaces them with direct V2 implementations in your normal
`~/.config/opencode`. No V1 SDK, generic compatibility bridge, alternate profile
or special launcher is needed afterward. Existing V1 sources are removed from
autoload/config entries and placed in one private safety snapshot before changes;
this is recovery evidence, not maintained V1 support. Auth and session databases
are not changed by the tool.

### Plugin decisions

| Plugin | Decision and reason |
| --- | --- |
| opencode-sandbox | Keep; native V2 shell hooks and maintained sandbox engine preserve host policy. Reported host allow/deny checks passed before and after reload; background cleanup remains a separate check. |
| python-isolation | Keep; pip/uv isolation remains useful, particularly with a readonly sandbox home. Private writable caches and native shell environment hook. |
| output-clamp | Keep; native 8/12 KiB selective UTF-8 budgets are stricter than V2's built-in 50 KiB/2,000-line limit. Full spill text, files and structured data are preserved. |
| lemonade-models | Keep; V2 does not automatically discover your configured compatible local server's live model inventory/capabilities/context. No models are loaded except explicit load/unload tool calls. |
| no-nested-sandbox | Keep; prevents models from hand-wrapping commands and nesting the host sandbox. Native original-command tracking avoids hook-order false positives. |
| pdf-to-text | Keep conditionally; native extraction is used only when the selected model does not advertise PDF support. Native PDF-capable routes receive the original media. |
| sandbox-uncloak | Retire from normal V2 use; the native sandbox clones execution input and leaves durable command history unwrapped. Old migrated conversations may still contain old wrappers; their history is not rewritten or deleted by this migration. |
| tool-definition-audit | Retire from normal use; its override map is empty, so it only performs repeated diagnostic serialization/file writes. Nonempty custom override maps require manual review rather than removal. |
| copilot-gpt6-effort | Retire; 2.2.6's Copilot catalog derives effort variants, sets reasoningEffort/reasoningSummary, and the native Responses model sends them. Selected effort was verified without the global-fetch/header workaround. |

The native sandbox port also waits for background shell completion events before
cleaning resources and reference-counts identical-policy instances. A conflicting
policy/runtime owner is blocked rather than silently reusing another project's
restrictions; use separate standalone servers for different policies. The native
PDF fallback covers primary, compaction, generate
and title requests, without V1 part/message conversion or persistent PDF logs.
Finish background shell jobs before unloading/reloading the sandbox plugin;
unload releases its proxy/runtime resources rather than promising persistent jobs.
No Build allow-all override is installed on the host. Unknown implementations,
V1 plugins or existing native destination directories require review instead of
being overwritten. The port targets the known implementations and 2.2.6; it is
not a generic arbitrary-plugin transpiler.

### Apply on the host

From the source checkout, first run the audit:

```sh
python3 tools/port_host_plugins.py
```

Local personal V1 implementations are not identified by their filename or a few
marker strings. Their canonical source bytes are not bundled with this tool.
An audit encountering such a source refuses until you privately review its full
contents against the proposed native implementation/retirement decision, then
explicitly approve its relative path and exact SHA-256. A digest approval records
your decision; it is not automatic compatibility certification. Do not approve
custom behavior that the native replacement does not preserve.

For example, after reviewing `plugins/output-clamp.ts`, obtain its digest without
printing its source or configuration values:

```sh
sha256sum "${XDG_CONFIG_HOME:-$HOME/.config}/opencode/plugins/output-clamp.ts"
```

Use the reviewed 64-hex digest in both audit and apply, repeating
`--reviewed-source RELATIVE_PATH=SHA256` for every reviewed local source:

```sh
python3 tools/port_host_plugins.py \
  --reviewed-source plugins/output-clamp.ts=REVIEWED_64_HEX_SHA256
```

Missing, changed, malformed, duplicate or unused approvals refuse before any
snapshot/install writes. Local paths are relative to the selected configuration
root, including a root selected by `--config`; do not pass an absolute path.
Active or nonliteral definition-audit override tables still require a manual
port and cannot be retired just by approving their digest. The bounded audit-table
check requires manual porting for executable identifier escapes, interpolated
templates and regex/division syntax outside quoted or comment data, even when a
particular use is benign. It does not evaluate JavaScript or certify arbitrary
semantics. Recognized configured local directories that do not have a
supported byte-bound implementation review are refused for manual porting, not
silently replaced by their basename. Unrelated scoped packages are not
reclassified by their basename. The explicitly supported
sandbox package spellings remain `opencode-sandbox`, `opencode-sandbox@latest`
and `opencode-sandbox@0.6.0`; other package versions require separate review.

Exit host OpenCode clients before applying to avoid reload during the file changes.
If using the shared service, stop it intentionally first; the tool itself never
starts/stops services. Then explicitly apply the reviewed native replacements:

```sh
python3 tools/port_host_plugins.py --apply \
  --reviewed-source plugins/output-clamp.ts=REVIEWED_64_HEX_SHA256
npm install --prefix "${XDG_CONFIG_HOME:-$HOME/.config}/opencode" \
  --no-audit --no-fund
```

The single-source example is illustrative: supply all approvals required by your
actual reviewed profile, or none for a recognized package-only profile with no
local sources to replace. Digest approvals are not reused automatically from a
previous invocation. The separate `--update-sandbox` operation below keeps its
receipt/source-hash checks and does not accept `--reviewed-source`.

The apply tool changes only recognized plugin sources, plugin entries and their
dependency keys. Unrelated config values and dependencies remain; JSONC comments
are preserved in the safety snapshot, while active files are reserialized. It
prints the private snapshot path and writes `native-plugin-migration.json`.
It refuses a second apply over the receipt; do not repeatedly overwrite a working
migration. If a filesystem/concurrent-edit failure interrupts application, keep
the printed safety snapshot and request recovery guidance rather than resetting
files or retrying blind.

Launch using the ordinary binary and your trusted env file:

```bash
( set -a; source /path/to/provider-credentials.env && set +a && opencode --standalone )
```

`opencode2` is an equivalent command alias in `@opencode/cli@2.2.6`. Select variants
through `/models` or `provider/model#variant`; merely setting the base/default
model does not select an effort level. Do not reintroduce the retired effort
plugin if the selected variant is wrong—verify the available catalog variants.

### The previous separate profile

If you ran `migrate_host_plugins.py --apply --output ~/.config/opencode-host-v2`
and installed its npm dependencies, **no original configuration needs reverting**.
That created only the separate profile and dependencies. Leave it unused while
testing the normal native config; its `run-opencode` launcher still selects the
older compatibility profile and should no longer be your normal launch command.
You may remove that one explicitly-created directory later after native host
validation. This tool does not recursively remove it or touch its files.

The npm `ini@7` engine warning is separate from plugin API migration. It requires
Node `^22.22.2`, `^24.15.0` or newer supported major; Node 24.14.0 is below that
24.x minimum. With fnm, select a current Node 24 release before dependency refresh.
Global npm tools are per Node installation, so switching Node may require
reinstalling the V2 CLI in the new installation:

```sh
fnm install 24
fnm use 24
npm install -g @opencode/cli@2.2.6
opencode --version
```

Use your existing package channel if the CLI is not npm-managed. Optional native
addon install-script warnings do not justify approving all dependency scripts;
inspect any package-specific need separately.

### Validation

```sh
python3 -B -m unittest discover -s tests -p test_native_host_port.py -v
python3 -B tests/smoke_native_host_port.py --opencode /path/to/opencode-v2
```

Native fixture checks run with no V1 SDK/bridge and verify inference/MCP, live
synthetic Lemonade inventory, unwrapped stored commands, selected reasoning
settings at a Responses endpoint and real PDF fallback bytes. The sandbox engine
is mocked inside this container; that is not host isolation proof. Reported host
foreground tests confirmed allowed project writes, denied outside-project writes,
an allowed PyPI request and an explicit example.com allowlist denial. A subsequent
host `/reload` initially failed closed before executing the probes. After the
lifecycle fix below, the operator repeated the probes before and after reload:
all child commands executed and the same filesystem/network enforcement passed.
Background-job cleanup is separately covered by mocked-runtime tests, not yet
reported validated on the host.
If the native sandbox is inactive or dependencies fail to load, do not assume
its fail-closed runtime hooks are present. Stop and fix activation first.

## Accepted native-host foreground and reload smoke results

All six native plugins were reported loaded in the normal host V2 setup. The
operator ran shell-tool tests with actual execution, not pre-execution OpenCode
permission refusals:

| Probe | Reported result |
| --- | --- |
| Write/read synthetic file inside disposable project | `sandbox-positive`, exit 0 |
| Create random empty file outside project in HOME | Child reported permission/read-only denial; no dummy file created |
| HTTPS request to allowed PyPI | HTTP 200, exit 0 |
| HTTPS request to disallowed example.com | curl exit 22; tunnel response HTTP 403 with `X-Proxy-Error: blocked-by-allowlist` |

After the sandbox-only lifecycle update and one fresh host launch, the same A–D
results were reported before and after `/reload`, without an initialization failure
or pre-execution permission refusal. The repeated denied request showed proxy
CONNECT 403/curl exit 22, not a DNS error, TLS failure or timeout. Reload is accepted
for this tested normal-host policy and disposable project, not just inferred from
mocked cleanup or plugin activation.

These are accepted foreground shell-tool smoke results for the tested policy,
paths and endpoints, not exhaustive sandbox certification. There were no config
changes, installations, real credential reads, or hand-written sandbox wrappers
in these probes. The successful project test file is disposable test data, not
source or evidence to commit. Repeat reload checks after lifecycle/policy changes;
finish background jobs first. Background completion/cleanup still needs its own
host test if relevant. Native file tools, MCPs, plugins and interactive PTYs
remain outside this shell-tool sandbox's claimed boundary.

## Sandbox lifecycle update after the first migration

The first host reload exposed a runtime ownership bug: sandbox-runtime 0.0.76
closes its proxy/helpers during `reset()` but retains `getConfig()`. The old
plugin deleted its lease record and then treated that idle cached config as an
unowned active runtime. The shell was correctly blocked in enforce mode, but
initialization could no longer recover on reload.

The fix keeps an idle ownership record tied to the exact runtime instance/policy
and waits for the old reset before initializing a new generation. It permits
policy changes only after the previous references are released, cleans up failed
registration, and makes cleanup idempotent. It never adopts/resets an unmarked
foreign runtime or makes enforce mode permissive. Failure messages give fixed
codes (`unowned-runtime`, `lease-generation`, `in-use-policy`, `runtime-init`,
`runtime-reset`, `runtime-owner-mismatch`, `event-stream`), not private exception
text or policy values.

From a **normal host terminal**, not the currently blocked OpenCode shell tool:

```sh
git pull --ff-only
python3 tools/port_host_plugins.py --update-sandbox
python3 tools/port_host_plugins.py --update-sandbox --apply
```

This hash-checks the installed generated sandbox, preserves its previous source
in a private file, updates only `plugins/opencode-sandbox/index.js` and the
migration receipt, and refuses customized code/dependency changes. Other plugins,
OpenCode config, auth/data and dependencies are untouched. No npm reinstall is
needed. Do not rerun the full `--apply` migration over an existing receipt.

Exit the old broken standalone service once and start a fresh host V2 process in
the same disposable project using your ordinary sourced env file. The old code
already discarded its ownership marker; the new code will not guess ownership
of that unmarked cached state. If using an unhealthy shared service instead,
restart it intentionally after closing its clients. This is one-time recovery
of the previously broken service, not a requirement to restart for future config
edits. Then run `/reload` and repeat the same allow/deny tests without another
restart. Three same-process native `/api/location/reload` cycles plus overlapping
reset/startup and registration-failure tests pass in offline fixtures; their
sandbox engine is mocked. The operator's subsequent host allow/deny retest before
and after reload passed as recorded above. Background behavior is not claimed
as validated by those foreground probes.

## Older option: separate compatibility profile

The remainder documents the previous optional profile approach. It is not needed
for a V2-only host and does not modify the normal host plugins.

The adapters inside oc-box do not edit your host plugins. Running
`opencode --standalone` outside oc-box therefore still loads your original V1
implementations, which V2 cannot execute.

`tools/migrate_host_plugins.py` creates an **independent host V2 configuration
profile**. It is a source-checkout utility, not an oc-box launcher command. It
does not edit the original config, replace installed OpenCode, change a database,
unlock credentials, or start a service/package manager. The default is a names-only
dry run. The `--apply` operation creates a new private profile and an explicit
launcher. Existing output directories are never replaced.

### Prepare a separate profile

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
refusal and cleanup pass mocked-runtime tests. Real host foreground enforcement
was reported passed for the normal native setup above, not exercised inside this
development container. Repeat these checks for changed policies/implementations:

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
