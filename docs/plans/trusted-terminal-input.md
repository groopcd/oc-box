# A host-owned input path for clipboard and file opening

The existing launcher relays bytes between a host terminal and the native
OpenCode client. On that input stream a decoded shortcut is not signed
evidence of a physical keypress. The relay must distinguish native reports,
clipboard pastes and opaque terminal replies; it cannot safely grant a host
read or desktop launch after an unknown boundary merely because input paused.
More recovery cases improve compatibility but do not change this constraint.

If real-terminal testing still finds routine lockouts, the next design to
prototype is an **opt-in host terminal frontend** (or an equivalent trusted
terminal integration), not a new unframed shortcut escape sequence.

- Own a focused host window and catch the real Ctrl+Y key event before
  terminal encoding, with distinct press/repeat handling. Similarly, match a
  Ctrl-click press/release in the focused window. No global interception.
- Deliver a one-shot action through a host-only in-process callback or a
  private inherited socketpair dedicated to the foreground client. The
  container, its stdout and terminal response handlers never get the
  action endpoint or a desktop connection. Refuse non-interactive,
  unfocused or unsupported clients.
- Reuse the guarded clipboard reader and bounded transfers. For Ctrl+Y,
  inject only a native paste at a verified clean delivery boundary; if busy
  or uncertain, refuse that press rather than restarting or queueing an
  automatic read. Never synthesize Enter. Preserve ordinary Ctrl+V.
- For a click, bind the host gesture to its client and screen position.
  Keep the existing native link hit-test and one-use artifact handoff;
  child output alone must never mint an authorization.

The initial test should use a disposable native workspace. Exercise real
pointer and keyboard events, focus loss, held modifiers, large paste,
rapid clicks, native tabs and live resize. Feed synthetic child traffic into
the frontend and verify that it cannot trigger a host action. Test desktop
clipboard and file-in-both-directions workflows separately from native PTY
fixtures. Only retain terminal compatibility that was exercised on the host.

This would be an alternative frontend, not a transparent repair to the
existing Yakuake pane. A KDE-hosted integration may fit, but its APIs,
packaging and trust properties are not yet verified. No terminal settings,
desktop shortcut, listener or new frontend is installed by the current
recovery work.
