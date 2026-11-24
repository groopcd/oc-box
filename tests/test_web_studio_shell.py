import hashlib
import unittest

from oc_box import web_sessions


URL = "http://127.0.0.1:12345"
ITEMS = [
    {"id": "ses_alpha", "title": "Synthetic <alpha>"},
    {"id": "ses_beta", "title": "Synthetic Beta"},
]


def fixture_state():
    state = web_sessions.catalog(URL, ITEMS, "valid")
    state.update(
        selection={**state["sessions"][0], "origin": "Synthetic explicit fixture"},
        selectionNotice="Synthetic selection",
    )
    return state


def fixture_document(state=None, addons=(), **options):
    return web_sessions.document(
        URL,
        ITEMS,
        "valid",
        state,
        addons,
        workspace_name="Synthetic workspace",
        **options,
    )


class StudioShellMarkupTests(unittest.TestCase):
    def test_ordinary_native_renderer_bytes_match_before_opt_in(self):
        state = fixture_state()
        cases = (
            (
                None,
                (),
                "60ee0c11c8fe340420f837a7b53d9c417e7fd06041639ef7bda9e377a71887e2",
            ),
            (
                state,
                (),
                "086fe483f41f0294f5f6975f99333034fded1daeb3b4622b9220ee20e8a49d40",
            ),
            (
                state,
                ("/auth/oc-box-attachments.js",),
                "deb9510e5103063d60e7c0f0852cb11678d631d0fde49aa4915aff08cd04e069",
            ),
        )
        for selection, addons, expected in cases:
            with self.subTest(selected=selection is not None, addons=bool(addons)):
                ordinary = fixture_document(selection, addons)
                self.assertEqual(hashlib.sha256(ordinary).hexdigest(), expected)
                self.assertNotIn(b"workspace-studio-center", ordinary)

    def test_explicit_opt_in_adds_only_inert_mount_markup(self):
        state = fixture_state()
        ordinary = fixture_document(state)
        opted = fixture_document(state, first_party_studio=True)
        mounts = b"""
    <div id="workspace-studio-center" hidden>
      <div id="workspace-studio-transcript"></div>
      <div id="workspace-studio-composer" hidden></div>
    </div>"""
        self.assertIn(b"</iframe>" + mounts, opted)
        self.assertEqual(opted.replace(mounts, b"", 1), ordinary)
        for bad in (None, 1, "true"):
            with self.subTest(value=bad), self.assertRaises(ValueError):
                fixture_document(state, first_party_studio=bad)
        with self.assertRaises(ValueError):
            fixture_document(
                state, ("/auth/oc-box-attachments.js",), first_party_studio=True
            )


if __name__ == "__main__":
    unittest.main()
