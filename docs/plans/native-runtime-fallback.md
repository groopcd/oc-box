# Known-working native OpenCode fallback

Status: not implemented. A working launch is not proof that the previous
OpenCode binary can reopen state written by a later one.

The safe first target is a failed **preflight** of a proposed OpenCode update,
before it can reach the project, private home volume, or credentials:

1. Stage and verify the exact native package in a disposable, unmounted
   environment. A different `HOME` in a container with live mounts is not
   isolation. Do not start the shared host service.
2. Record an exact runtime as known-working only from actual private owner
   readiness, including the Build plugin, tied to the same immutable
   container and home volume. Version strings or package presence alone do
   not qualify it.
3. Before any new native process can open live state, durably record that it
   may have written it. A surviving old binary or missing readiness frame
   cannot clear this fence. Revalidate resource identity and stop intent
   before selecting the retained runtime, not just before downloading.
4. On an eligible preflight failure, warn and attempt the verified prior
   runtime through ordinary owning startup. Require tested host/container
   credential and history interoperability; do not waive the exact-version
   check for post-stop history recovery.

A failure during real migration, auth refresh, plugin activation, or service
startup is later than that boundary. Recovering there requires a separately
verified state rollback with no lost history or replayed plugin, MCP, or job
side effects. An executable downgrade by itself is not that recovery.
