# How oc-box works

[Documentation](README.md) / Concepts

> **OpenCode 2.x architecture.** Legacy 1.x keeps separate operating guidance
> for [credentials, plugins and terminal behavior](legacy/README.md).

This page explains the environment and permission model. For commands, use
[workspace management](guides/workspaces.md) or the [CLI reference](cli.md).
For the full threat model and reporting process, read [Security](../SECURITY.md).

## A persistent environment, not a patched OpenCode

oc-box grew out of wanting OpenCode to have more freedom without unrestricted
host access. A host Bubblewrap sandbox can restrict commands, but installing
packages and experimenting with services can mean working around those host
restrictions. oc-box instead gives OpenCode a persistent Linux environment where
it can install tools, change system configuration and run services.

OpenCode stays on mainline without patches. The container matches the host's
installed OpenCode version. MCP servers, formatters, plugins and tools run
natively inside the same container. Upstream 2.2.6 does not provide native LSP;
check the [version-specific capability map](opencode-2.md#compatibility-map).
Legacy 1.x native language servers are documented in the
[legacy integration guide](legacy/README.md#plugins-and-language-servers).

Rootless containers share the host kernel, have network access and have no disk
quota. They are replaceable development environments, not virtual machines or
a promise that mounted data cannot be lost.

## Storage and lifetime

| Area | What it contains |
| --- | --- |
| Writable project mount | Source, Git history, hidden files and unfinished edits at the same absolute path |
| Private named home volume | Live OpenCode history, session IDs, home-installed tools and caches; not the host home |
| Container OS layer | System packages and files outside the project/home, retained until reset/removal |
| Host history archives | Verified conversation exports, separate from the live private database |

Stopping keeps files but not running processes. Reset rebuilds the OS while
retaining project and private home; purge has different deletion semantics.
See the [operation table](guides/workspaces.md) before acting. Conversation
archives are not backups against disk/volume loss.

## Configuration and integrations

Global OpenCode configuration, skills, agents, plugins, MCP settings and auth
are copied into private storage at startup. Host configuration changes apply
on the next launch. Host-only executables must also be installed in the container.

The host Bubblewrap plugin is disabled for container launch rather than nesting
a second sandbox. HTTP endpoints using localhost in global configuration are
mapped to `host.containers.internal`, including the host Lemonade endpoint.
Plain host OpenCode retains its normal configuration and permissions.

V2 uses a private foreground workspace service rather than the shared host
service. For plain host `opencode --standalone`, original V1 plugins are not
automatically adapted. The optional [host plugin utility](host-plugins.md)
audits and ports known plugins; it is a source-checkout tool, not an installed
oc-box command or proof of host sandbox enforcement.

## Container Build permissions

V2 retains the existing final Build-agent wildcard override of copied ordinary
global/Build tool permission rules, including their configured denies.
Final permission asks for the actual container Build agent are auto-allowed,
but explicit session/mandatory-policy denials and managed rules applied after
the private overlay remain denied. Unknown agents retain native ask/deny
outcomes; they are not treated as Build. Plan/child behavior is unchanged.

The V2 helper changes only a final `ask` to `allow`, never a deny passed to it.
An explicit event agent wins; otherwise the current session's agent and then
native configured default are used, never a parent's agent. A Build parent
does not grant allowances to Plan/child requests. Session permission arrays
are not overwritten; question/MCP elicitation prompts are separate and may
still need input.

The final private `OPENCODE_CONFIG_CONTENT` overlay reenables only the generated
Build helper against lower-priority project plugin removals. Managed policy
still wins. oc-box clients do not receive `--auto` or blanket client autoaccept.

For an intentional one-off **plain host** launch, native
`opencode --auto --standalone` uses host env/auth, not the oc-box vault. It
approves asks, not explicit denials, including host configured denies. The
native TUI equivalent is Ctrl+P → Open settings → Sessions → Permissions →
autoaccept (`cli.json` `session.permissions`); that broader preference can
persist and is not enabled by oc-box. It is not a substitute for the container
Build-only policy.

Native evaluator/import/reload fixtures are offline checks, not new live
interactive permission-prompt/reload acceptance. The separate scripted
global-vault host fixture passed native Build/scoped-permission/reload checks
at its [recorded scope](opencode-2.md#isolated-global-vault-and-build-approval-host-validation).

## Credentials and disclosure

Default V2 global-vault mode supplies every current entry on a new owner and
replaces automatic ambient token/API-key and `pass_env` forwarding when a vault
is present, including legacy `OPENCODE_AUTH_CONTENT` overrides. Native saved/OAuth
refresh/copy is independent: vault env entries do not replace structured OAuth
records. Explicit env policies and V2 without a vault retain their documented
fallback behavior.

Delivered credentials are readable by the agent/tools. Copied auth/config may
already contain secrets, and changing policy does not revoke earlier copies.
The vault/passphrase are never mounted; forwarded vault values do not enter
Podman argv, host `os.environ`, configuration or registry metadata.
Use [selected policies](guides/credentials.md) when a project should receive less.

Host drops, image snapshots and live file tools have different consent and
retention boundaries. Start with the [host-file workflow comparison](guides/host-files.md),
not the assumption that read-only access prevents disclosure.

## Related reading

- [Security and trust boundary](../SECURITY.md).
- [OpenCode 2.x compatibility and history strategy](opencode-2.md).
- [Workspace lifecycle and recovery](guides/workspaces.md).
- [Current status and validation boundaries](project-status.md).
