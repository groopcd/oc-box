import hashlib
import json
import unittest
from html.parser import HTMLParser
from unittest.mock import patch

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


class Page(HTMLParser):
    def __init__(self, body):
        super().__init__()
        self.tags = []
        self.feed(body.decode())

    def handle_starttag(self, name, attrs):
        self.tags.append((name, dict(attrs)))


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

    def test_explicit_named_uses_same_chrome_but_no_native_authority_or_route(self):
        private = "synthetic-never-disclosed-marker"
        sessions = [
            {
                **item,
                "route": "/server/" + private,
                "authorization": private,
                "location": {"directory": "/path/" + private},
            }
            for item in ITEMS
        ]
        with patch.object(
            web_sessions, "native_session_route", side_effect=AssertionError
        ):
            body = web_sessions.document(
                "http://" + private,
                sessions,
                "valid",
                workspace_name="Named <workspace>",
                named_studio=True,
            )
        self.assertNotIn(private.encode(), body)
        tags = Page(body).tags
        ids = {attrs["id"]: attrs for _, attrs in tags if "id" in attrs}
        self.assertIn("data-named-studio", ids["web-shell"])
        self.assertEqual(
            json.loads(ids["web-shell"]["data-bootstrap"]),
            {
                "status": "valid",
                "sessions": ITEMS,
                "selection": None,
                "selectionNotice": "Choose a verified session from this workspace.",
            },
        )
        for name in ("iframe", "a", "form", "textarea"):
            self.assertNotIn(name, [tag for tag, _ in tags])
        prefix = web_sessions.NAMED_STUDIO_ASSET_PREFIX
        self.assertEqual(
            [attrs["href"] for tag, attrs in tags if tag == "link"],
            [prefix + "web_sessions.css", prefix + "web_studio_shell.css"],
        )
        self.assertEqual(
            [attrs["src"] for tag, attrs in tags if tag == "script"],
            [prefix + "web_sessions.js"],
        )
        self.assertTrue(
            {
                "workspace-masthead",
                "workspace-library",
                "workspace-chat",
                "workspace-overview",
                "workspace-manager",
                "workspace-studio-center",
                "workspace-studio-composer",
                "workspace-studio-transcript",
            }
            <= ids.keys()
        )
        self.assertIn(b"Named &lt;workspace&gt;", body)
        self.assertIn(b"Native OpenCode: host terminal", body)
        self.assertNotIn("session-controls", ids)
        self.assertNotIn("fallback-links", ids)
        self.assertNotIn(b"/auth/", body)

    def test_named_refuses_legacy_authority_and_invalid_catalog(self):
        for options in (
            {"named_studio": "true"},
            {"named_studio": True, "first_party_studio": True},
            {"named_studio": True, "addons": ("/auth/oc-box-attachments.js",)},
            {"named_studio": True, "state": fixture_state()},
        ):
            with self.subTest(options=tuple(options)), self.assertRaises(ValueError):
                fixture_document(**options)
        for records, status in (
            (ITEMS + ITEMS, "valid"),
            ([{"id": "ses_escape\n"}], "valid"),
            (ITEMS, "missing"),
            (ITEMS, "unavailable"),
            ([], "unknown"),
        ):
            with self.subTest(status=status), self.assertRaises(ValueError):
                web_sessions.document(None, records, status, named_studio=True)


if __name__ == "__main__":
    unittest.main()
