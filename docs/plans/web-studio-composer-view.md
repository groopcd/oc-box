# Standalone Studio composer: Chromium smoke receipt

The source-only composer at `a7f7726cbf145e4efb42d5a81f635615bed24165`
was tested with its sibling [read-only transcript](web-studio-transcript-view.md)
at **2269 × 1398 CSS pixels**, scale factor 1, in Chrome Headless Shell
**159.0.8154.6** with Playwright **1.65.2**. The approved comparison is the
unmodified `21e966ce80d2dde4db853c5a784ef10b28d90af4:docs/mockups/web-manager.html`.

| Input | SHA-256 |
|---|---|
| Approved mockup | `cf044d2a71efc9220fc6b2be6c4e3651e44d6dfce88f70390fe8645cc5941af9` |
| `tests/smoke_web_studio_composer_browser.py` | `2ab2a26932f38a8e305c3e5a97feb37b3a8b7e45c01d4f936e76b5f0d88fc5bd` |
| Chrome Headless Shell executable | `24eb0a14de64ab0c9c2e283a5512ca817db64af8fd853cdb4d818f19a8e23b9b` |

- **Geometry and styling:** At the approved center's position and width the
  composer box matched exactly: left **763.5**, top **1259.5**, width **712**,
  height **111** CSS px. The smoke also compares center, header, field, footer,
  right-aligned enabled Send, icon, and note geometry or computed styles where
  the two layouts are comparable. The live view has no demo form, model/agent
  tags, or fake actions. It uses real status and disabled states; the Send
  label and explanatory copy are intentionally different. This is not a
  pixel comparison of the whole Studio shell.
- **Activation:** Chromium marked the Playwright pointer click and focused
  button's Space activation as trusted. Scripted clicks, synthetic keyboard
  events, Enter in the draft, and a trusted click on programmatically replaced
  text did not reach the adapter. Disabled clicks did not send a duplicate.
- **Drafts and receipts:** Transcript replacement kept the draft. A queued
  acknowledgement did not imply execution or erase a newer revision even when
  that revision had identical text. A confirmed refusal kept the draft and
  allowed another explicit click; an unconfirmed reply preserved the draft but
  blocked another Send for that generation. Hash navigation away and back
  replaced the old field; late replies did not change the new draft or status.
  Silent fixture generation changes were checked both before and after Send.
- **Isolation:** Only seven intercepted startup GETs were observed. No later
  HTTP request, WebSocket, form submission, draft in `FormData`, local/session
  storage entry, or mutation of the sibling `srcdoc` iframe or its draft was
  observed. The browser and its private temporary home were disposable.

Run from the repository root with a local browser and cached Playwright:

```sh
uv run --offline --no-project --with playwright python -B \
  tests/smoke_web_studio_composer_browser.py --chromium /path/to/chromium
```

Both documents and all assets are fulfilled by the browser's fixture route.
The Send adapter is a deferred synthetic callback; the navigation is
same-document fixture navigation. The receipt does not cover a live shell,
native queue admission, one-use/named transport, inference, or Podman.
