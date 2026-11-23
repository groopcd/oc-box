"""Check the profile glue without shipping a native verifier."""

import unittest
from unittest.mock import Mock, patch

from oc_box import web_local_origins, web_pake_profile, web_pake_worker


class LocalPakeProfileTests(unittest.TestCase):
    def test_worker_receives_separate_fixed_identifiers_and_profile_context(self):
        origins = web_local_origins.LocalOrigins()
        digest = b"d" * 32
        context = b"c" * 32
        with patch.object(web_pake_worker, "PakeWorker") as constructor:
            profile = web_pake_profile.LocalPakeProfile(
                42, expected_sha256=digest, origins=origins
            )
            worker = constructor.return_value
            handle = Mock()
            worker.start.return_value = (b"reply", handle)
            reply = profile.start(
                setup=b"setup",
                credential_identifier=b"sample-account",
                context=context,
                registration=None,
                ke1=b"request",
                deadline=123.5,
            )
            self.assertEqual(reply, (b"reply", handle))
            args = worker.start.call_args.kwargs
            ids = [
                args[name]
                for name in (
                    "credential_identifier",
                    "client_identifier",
                    "verifier_identifier",
                )
            ]
            self.assertEqual(len(set(ids)), 3)
            self.assertTrue(all(len(ident) == 32 for ident in ids))
            self.assertEqual(args["context"], context)
            self.assertIsNone(args["registration"])
            self.assertEqual(args["deadline"], 123.5)
            worker.close.return_value = False
            self.assertFalse(profile.close())
        constructor.assert_called_once_with(
            42, expected_sha256=digest, sizes=web_pake_worker.LAB_SIZES
        )
        self.assertEqual(profile.sizes.ke2, 320)
