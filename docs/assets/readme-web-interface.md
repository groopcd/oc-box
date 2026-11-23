# Browser Workspace manager preview

`readme-web-interface.png` is a static documentation rendering of **Workspace
manager → Sessions**, not a live browser or Podman screenshot. It uses
[the production web shell at `96ee684`](https://github.com/groopcd/oc-box/tree/96ee684b91d852544711672d0d5710d57c7eb615/src/oc_box):
`web_sessions.document` renders the markup, `web_sessions.js` selects the
manager layout and its Sessions section and populates a synthetic three-session
catalog, and `web_sessions.css` supplies the layout. The static renderer uses
a dark palette and print-layout adjustments. No network request, native
conversation, credentials or host data was used.

The browser manager exposes TUI-style section navigation, not a live Textual
terminal or host manager controls. Unsupported sections display an unavailable
state. See the [development web shell guide](https://github.com/groopcd/oc-box/blob/devel/docs/web-shell.md)
for the live feature boundaries. The previous Studio image was a hand-drawn SVG
illustration, not an external image or screenshot.
