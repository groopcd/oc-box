# Browser Dashboard screenshot

`readme-web-interface.png` is a Firefox 159.0.1 viewport screenshot of the
**Dashboard** tab, captured with WebDriver BiDi at 1440 × 810. It shows
[the production oc-box browser shell and gateway at `9b48657`](https://github.com/groopcd/oc-box/tree/9b4865789637b9c5c176affca3b7f7433e76c3ea/src/oc_box)
with one unmodified native OpenCode 2.4.1 conversation.

The capture used a fresh, synthetic workspace and a private native server,
with native one-use browser pairing. Only the outer Podman exec transport was
replaced with local processes. The session transcript was imported through the
native API, and a fixture supplied the three open TUI tab identities; no
provider inference or live TUI session was used. No real project, credentials
or host metadata appears in the image. This is a browser documentation
screenshot, not a live rootless Podman acceptance run.

See the [development web shell guide](https://github.com/groopcd/oc-box/blob/devel/docs/web-shell.md)
for the layout and data boundaries.
