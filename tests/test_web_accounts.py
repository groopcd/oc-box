import base64
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import fcntl
import importlib.util
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

from oc_box import web_accounts as accounts


HAS_CRYPTO = importlib.util.find_spec("cryptography") is not None
PHRASE = "synthetic store unlock phrase"
USERNAME = "synthetic-account"
WORKSPACE = "1234567890abcdef" * 2
OTHER_WORKSPACE = "fedcba0987654321" * 2
SETUP = b"\xff\0synthetic-protocol-server-setup"
REGISTRATION = b"\xfe\0synthetic-protocol-registration"


class StoreFixture(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="oc-box-web-accounts-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.directory = self.root / "private"
        self.directory.mkdir(mode=0o700)
        self.path = self.directory / "accounts.store"
        environment = patch.dict(os.environ, {"OC_BOX_CONTAINER": "0"})
        environment.start()
        self.addCleanup(environment.stop)

    def create(self, *, setup=SETUP, phrase=PHRASE):
        accounts.initialize(self.path, phrase, setup=setup)

    def add(
        self,
        name=USERNAME,
        *,
        workspace_ids=(WORKSPACE,),
        record=REGISTRATION,
        phrase=PHRASE,
    ):
        accounts.add_account(
            self.path, phrase, name, workspace_ids=workspace_ids, registration=record
        )

    def write_fixture(self, raw):
        self.path.touch(mode=0o600, exist_ok=True)
        self.path.write_bytes(raw)

    def assert_unchanged(self, raw):
        self.assertEqual(self.path.read_bytes(), raw)
        self.assertEqual(list(self.directory.iterdir()), [self.path])


class StoreRefusalTests(StoreFixture):
    def test_absence_and_invalid_paths_never_create_state(self):
        with self.assertRaisesRegex(accounts.WebAccountsError, "not initialized"):
            accounts.load(self.path, PHRASE)
        with self.assertRaisesRegex(accounts.WebAccountsError, "not initialized"):
            accounts.load_snapshot(self.path, PHRASE)
        with self.assertRaisesRegex(accounts.WebAccountsError, "not initialized"):
            self.add()
        with self.assertRaisesRegex(accounts.WebAccountsError, "not initialized"):
            accounts.remove_account(self.path, PHRASE, USERNAME)
        for target in (
            "some/file",
            Path("relative"),
            self.directory / "../elsewhere",
            self.root / "missing/accounts.store",
            self.directory / "nul\0",
        ):
            with (
                self.subTest(target=target),
                self.assertRaises(accounts.WebAccountsError),
            ):
                accounts.initialize(target, PHRASE, setup=SETUP)
        self.assertEqual(list(self.root.iterdir()), [self.directory])
        self.assertEqual(list(self.directory.iterdir()), [])

    def test_all_operations_refuse_container_before_filesystem_access(self):
        with (
            patch.dict(os.environ, {"OC_BOX_CONTAINER": "1"}),
            patch.object(accounts, "_open_directory", side_effect=AssertionError),
        ):
            for operation in (
                self.create,
                lambda: accounts.load(self.path, PHRASE),
                lambda: accounts.load_snapshot(self.path, PHRASE),
                self.add,
                lambda: accounts.remove_account(self.path, PHRASE, USERNAME),
            ):
                with self.assertRaisesRegex(accounts.WebAccountsError, "host-only"):
                    operation()

    def test_names_scopes_and_protocol_records_have_closed_shapes(self):
        for name in (
            "",
            "../ab",
            "UPPER",
            "_prefix",
            "áccount",
            "abc\n",
            "a" * 33,
            b"name",
            None,
        ):
            with (
                self.subTest(name=name),
                self.assertRaisesRegex(accounts.WebAccountsError, "usernames"),
            ):
                self.add(name)
        for scope in (
            (),
            ("*",),
            (WORKSPACE.upper(),),
            (WORKSPACE + "\n",),
            ("a" * 31,),
            ("../bad",),
            (WORKSPACE, WORKSPACE),
            ("a" * 32, None),
            WORKSPACE,
            {WORKSPACE},
            [f"{ident:032x}" for ident in range(accounts.MAX_WORKSPACES + 1)],
        ):
            with (
                self.subTest(scope=scope),
                self.assertRaisesRegex(accounts.WebAccountsError, "distinct, explicit"),
            ):
                self.add(workspace_ids=scope)
        for invalid in ("text-not-protocol-bytes", b"", bytearray(b"binary"), None):
            with self.assertRaisesRegex(accounts.WebAccountsError, "protocol record"):
                self.add(record=invalid)
            with self.assertRaisesRegex(accounts.WebAccountsError, "protocol record"):
                self.create(setup=invalid)
        with self.assertRaisesRegex(accounts.WebAccountsError, "protocol record"):
            self.add(record=b"a" * (accounts.MAX_REGISTRATION_BYTES + 1))
        with self.assertRaisesRegex(accounts.WebAccountsError, "protocol record"):
            self.create(setup=b"a" * (accounts.MAX_SETUP_BYTES + 1))
        self.assertEqual(list(self.directory.iterdir()), [])

    def test_invalid_passphrases_not_echoed_and_cannot_create_store(self):
        for phrase in (
            "",
            b"not-text",
            "7-chars",
            "pass\0phrase",
            "\ud800" * 8,
            "é" * 513,
        ):
            with (
                self.subTest(phrase_type=type(phrase)),
                self.assertRaisesRegex(
                    accounts.WebAccountsError, "passphrase"
                ) as raised,
            ):
                self.create(phrase=phrase)
            self.assertTrue(
                raised.exception.__suppress_context__
                or raised.exception.__context__ is None
            )
        self.assertEqual(list(self.directory.iterdir()), [])

    def test_refuses_symlink_parents_and_nonprivate_directory(self):
        link = self.root / "link"
        link.symlink_to(self.directory, target_is_directory=True)
        inner = self.directory / "inner"
        inner.mkdir(mode=0o700)
        for target in (link / self.path.name, link / "inner" / self.path.name):
            with self.assertRaisesRegex(accounts.WebAccountsError, "unsafe"):
                accounts.initialize(target, PHRASE, setup=SETUP)
        self.assertEqual(list(inner.iterdir()), [])
        self.directory.chmod(0o755)
        with self.assertRaisesRegex(accounts.WebAccountsError, "unsafe"):
            self.create()
        self.assertEqual(list(self.directory.iterdir()), [inner])

    def test_refuses_unsafe_entries_without_crypto_or_replacement(self):
        outside = self.root / "outside"
        outside.write_bytes(b"unrelated file")
        outside.chmod(0o600)
        for kind in ("symlink", "hardlink", "fifo", "wide mode", "oversized"):
            with self.subTest(kind=kind):
                if kind == "symlink":
                    self.path.symlink_to(outside)
                elif kind == "hardlink":
                    self.path.hardlink_to(outside)
                elif kind == "fifo":
                    os.mkfifo(self.path, 0o600)
                else:
                    self.write_fixture(b"unrelated")
                    if kind == "wide mode":
                        self.path.chmod(0o640)
                    else:
                        with self.path.open("r+b") as stream:
                            stream.truncate(accounts.MAX_FILE + 1)
                info = self.path.lstat()
                with patch.object(accounts, "_cipher", side_effect=AssertionError):
                    with self.assertRaisesRegex(accounts.WebAccountsError, "unsafe"):
                        accounts.load(self.path, PHRASE)
                    with self.assertRaisesRegex(accounts.WebAccountsError, "unsafe"):
                        self.add()
                    with self.assertRaisesRegex(
                        accounts.WebAccountsError, "not overwrite"
                    ):
                        self.create()
                self.assertEqual(info, self.path.lstat())
                self.path.unlink()
        self.assertEqual(outside.read_bytes(), b"unrelated file")

    def test_lock_wait_is_bounded_and_read_creates_no_lock_file(self):
        fd = os.open(self.directory, os.O_RDONLY | os.O_DIRECTORY)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with (
                patch.object(accounts, "_LOCK_TIMEOUT", 0.02),
                self.assertRaisesRegex(accounts.WebAccountsError, "busy"),
            ):
                accounts.load(self.path, PHRASE)
        finally:
            os.close(fd)
        self.assertEqual(list(self.directory.iterdir()), [])

    def test_missing_optional_crypto_is_a_fixed_refusal_and_leaves_nothing(self):
        def import_without_crypto(name, *args, **kwargs):
            if name.startswith("cryptography."):
                raise ImportError("private dependency detail")
            return original_import(name, *args, **kwargs)

        original_import = __import__
        with (
            patch("builtins.__import__", side_effect=import_without_crypto),
            self.assertRaisesRegex(
                accounts.WebAccountsError, "optional cryptography"
            ) as raised,
        ):
            self.create()
        self.assertNotIn("private dependency detail", str(raised.exception))
        self.assertEqual(list(self.directory.iterdir()), [])


@unittest.skipUnless(HAS_CRYPTO, "optional cryptography not installed")
class AccountSnapshotTests(StoreFixture):
    def test_authenticates_once_and_close_fences_a_retained_callback(self):
        self.create()
        self.add()
        raw = self.path.read_bytes()
        with (
            patch.object(
                accounts, "_open_envelope", wraps=accounts._open_envelope
            ) as unlock,
            patch.object(accounts, "_locked", wraps=accounts._locked) as exclusive,
            accounts.load_snapshot(self.path, PHRASE) as snapshot,
        ):
            self.assertEqual(
                snapshot.store,
                accounts.AccountStore(
                    SETUP, (accounts.Account(USERNAME, (WORKSPACE,), REGISTRATION),)
                ),
            )
            current = snapshot.current  # The callable intended for prepare_sites.
            guard = current.__self__
            descriptors = (guard._dir, guard._file)
            self.assertIsNot(guard, snapshot)
            self.assertTrue(all(not os.get_inheritable(fd) for fd in descriptors))
            for _ in range(3):
                self.assertIs(current(), True)
            unlock.assert_called_once()
            exclusive.assert_called_once()
            self.assertNotIn(USERNAME, repr(snapshot))
            self.assertNotIn(str(self.path), repr(snapshot))
        for fd in descriptors:
            with self.assertRaises(OSError):
                os.fstat(fd)
        snapshot.close()
        with patch.object(accounts, "_check_directory") as inspect:
            self.assertIs(current(), False)
            inspect.assert_not_called()
        self.assert_unchanged(raw)

    def test_wrong_passphrase_and_damaged_ciphertext_never_return_a_guard(self):
        self.create()
        raw = self.path.read_bytes()
        with self.assertRaisesRegex(accounts.WebAccountsError, "incorrect passphrase"):
            accounts.load_snapshot(self.path, "another synthetic phrase")
        self.assert_unchanged(raw)
        damaged = raw[:-1] + bytes([raw[-1] ^ 1])
        self.path.write_bytes(damaged)
        with self.assertRaisesRegex(accounts.WebAccountsError, "damaged data"):
            accounts.load_snapshot(self.path, PHRASE)
        self.assert_unchanged(damaged)

    def test_load_refuses_a_replacement_during_authentication(self):
        self.create()
        original = self.path.read_bytes()
        replacement = self.directory / "synthetic-racing-replacement"
        replacement.touch(mode=0o600)
        replacement.write_bytes(original)
        unlock = accounts._open_envelope

        def unlock_and_replace(*args):
            result = unlock(*args)
            os.replace(replacement, self.path)
            return result

        with (
            patch.object(accounts, "_open_envelope", side_effect=unlock_and_replace),
            self.assertRaisesRegex(accounts.WebAccountsError, "unsafe"),
        ):
            accounts.load_snapshot(self.path, PHRASE)
        self.assert_unchanged(original)

    def test_in_place_change_with_preserved_size_and_mtime_is_sticky(self):
        self.create()
        original = self.path.read_bytes()
        modified = original[:-1] + bytes([original[-1] ^ 1])
        with accounts.load_snapshot(self.path, PHRASE) as snapshot:
            before = self.path.stat()
            self.path.write_bytes(modified)
            os.utime(self.path, ns=(before.st_atime_ns, before.st_mtime_ns))
            after = self.path.stat()
            self.assertEqual(
                (before.st_ino, before.st_size, before.st_mtime_ns),
                (after.st_ino, after.st_size, after.st_mtime_ns),
            )
            self.assertIs(snapshot.current(), False)
            self.path.write_bytes(original)
            self.assertIs(snapshot.current(), False)

    def test_digest_refuses_in_place_change_even_if_observed_times_do_not_change(self):
        self.create()
        original = self.path.read_bytes()
        stamp = accounts._stamp

        def fixed_times(info):
            value = stamp(info)
            return None if value is None else value[:-2]

        # Model a check whose observed timestamps alone cannot reveal an edit.
        with (
            patch.object(accounts, "_stamp", side_effect=fixed_times),
            accounts.load_snapshot(self.path, PHRASE) as snapshot,
        ):
            self.assertIs(snapshot.current(), True)
            self.path.write_bytes(bytes([original[0] ^ 1]) + original[1:])
            with patch.object(accounts.os, "pread", wraps=os.pread) as read:
                self.assertIs(snapshot.current(), False)
                read.assert_called()

    def test_byte_identical_atomic_file_replacement_is_not_read_or_adopted(self):
        self.create()
        original = self.path.read_bytes()
        with accounts.load_snapshot(self.path, PHRASE) as snapshot:
            replacement = self.directory / "synthetic-replacement"
            replacement.touch(mode=0o600)
            replacement.write_bytes(original)
            os.replace(replacement, self.path)
            with patch.object(accounts.os, "pread", wraps=os.pread) as read:
                self.assertIs(snapshot.current(), False)
                read.assert_not_called()
            self.assert_unchanged(original)

    def test_parent_swaps_refuse_before_read_even_with_matching_filename(self):
        self.create()
        original = self.path.read_bytes()
        displaced = self.root / "displaced"
        with accounts.load_snapshot(self.path, PHRASE) as snapshot:
            self.directory.rename(displaced)
            self.directory.mkdir(mode=0o700)
            self.write_fixture(original)
            with patch.object(accounts.os, "pread", wraps=os.pread) as read:
                self.assertIs(snapshot.current(), False)
                read.assert_not_called()
        with accounts.load_snapshot(self.path, PHRASE) as snapshot:
            second = self.root / "second"
            self.directory.rename(second)
            self.directory.symlink_to(second, target_is_directory=True)
            with patch.object(accounts.os, "pread", wraps=os.pread) as read:
                self.assertIs(snapshot.current(), False)
                read.assert_not_called()

    def test_path_replaced_during_read_is_rechecked_and_only_original_fd_is_read(self):
        self.create()
        original = self.path.read_bytes()
        before = self.path.stat()
        replacement = self.directory / "pending"
        replacement.touch(mode=0o600)
        replacement.write_bytes(original)
        pread = os.pread
        replaced = False
        identities = []

        def substitute(fd, count, offset):
            nonlocal replaced
            opened = os.fstat(fd)
            identities.append((opened.st_ino, opened.st_dev))
            if not replaced:
                os.replace(replacement, self.path)
                replaced = True
            return pread(fd, count, offset)

        with accounts.load_snapshot(self.path, PHRASE) as snapshot:
            with patch.object(accounts.os, "pread", side_effect=substitute):
                self.assertIs(snapshot.current(), False)
            self.assertTrue(replaced)
            self.assertEqual(set(identities), {(before.st_ino, before.st_dev)})
            self.assert_unchanged(original)

    def test_guard_does_not_hold_or_wait_for_lock_and_enrollment_fences_it(self):
        self.create()
        with (
            accounts.load_snapshot(self.path, PHRASE) as snapshot,
            patch.object(accounts, "_LOCK_TIMEOUT", 0.02),
        ):
            review = accounts.review_registration(
                self.path, PHRASE, USERNAME, workspace_ids=(WORKSPACE,)
            )
            accounts.commit_registration(
                self.path,
                PHRASE,
                review,
                registration=REGISTRATION,
                current=snapshot.current,
            )
            self.assertFalse(snapshot.current())
            self.assertEqual(snapshot.store.accounts, ())
        with accounts.load_snapshot(self.path, PHRASE) as reopened:
            self.assertTrue(reopened.current())
            self.assertEqual(reopened.store.accounts[0].registration, REGISTRATION)

    def test_storage_errors_and_container_use_refuse_without_rechecking(self):
        self.create()
        with accounts.load_snapshot(self.path, PHRASE) as snapshot:
            with patch.object(
                accounts.os, "pread", side_effect=OSError("private file error")
            ):
                self.assertIs(snapshot.current(), False)
            self.assertIs(snapshot.current(), False)
        with accounts.load_snapshot(self.path, PHRASE) as snapshot:
            with (
                patch.dict(os.environ, {"OC_BOX_CONTAINER": "1"}),
                patch.object(accounts, "_check_directory") as inspect,
            ):
                self.assertIs(snapshot.current(), False)
                inspect.assert_not_called()
            self.assertIs(snapshot.current(), False)


class HeldRegistrationTests(StoreFixture):
    """Real store framing, lock and file publication; synthetic envelopes."""

    before = b"synthetic old ciphertext"
    after = b"synthetic new ciphertext"

    def setUp(self):
        super().setUp()
        self.write_fixture(self.before)
        store = accounts.AccountStore(SETUP, ())
        authentication = patch.object(accounts, "_open_envelope", return_value=store)
        sealing = patch.object(accounts, "_seal", return_value=self.after)
        self.authentication = authentication.start()
        self.sealing = sealing.start()
        self.addCleanup(authentication.stop)
        self.addCleanup(sealing.stop)

    def hold(self):
        return accounts._hold_registration(
            self.path, PHRASE, USERNAME, workspace_ids=(WORKSPACE,)
        )

    def commit(self, transaction, review, *, current=lambda: True):
        transaction.commit(PHRASE, review, registration=REGISTRATION, current=current)

    def competing_initializer(self, stage, *, excluded=True):
        # Use the existing, unmodified writer API in a separate process that
        # cannot inherit the locked descriptor. Existing-store refusal avoids
        # invoking optional crypto if the contender did acquire the lock.
        program = """
import sys
from pathlib import Path

sys.path.insert(0, sys.argv[1])
from oc_box import web_accounts

web_accounts._LOCK_TIMEOUT = 0.035
try:
    web_accounts.initialize(
        Path(sys.argv[2]), "synthetic competing phrase", setup=b"synthetic setup"
    )
except web_accounts.WebAccountsError as exc:
    if str(exc) == "Web account storage is busy; retry later.":
        sys.exit(10)
    if str(exc) == (
        "Web account store already exists; initialization will not overwrite it."
    ):
        sys.exit(11)
    sys.exit(12)
sys.exit(13)
"""
        result = subprocess.run(
            [
                sys.executable,
                "-I",
                "-B",
                "-c",
                program,
                str(Path(accounts.__file__).resolve().parents[1]),
                str(self.path),
            ],
            env={"OC_BOX_CONTAINER": "0"},
            cwd=self.root,
            capture_output=True,
            timeout=5,
        )
        self.assertEqual(result.returncode, 10 if excluded else 11, stage)
        self.assertEqual((result.stdout, result.stderr), (b"", b""), stage)

    def test_existing_writer_is_excluded_from_review_through_publication_postcheck(
        self,
    ):
        stages = []
        descriptors = set()
        prior_check = accounts._unchanged

        def recheck(path, directory, expected):
            descriptors.add(directory)
            prior_check(path, directory, expected)
            if self.path.read_bytes() == self.after:
                self.competing_initializer("published; doing postcheck")
                stages.append("postcheck")

        def at_authentication(_raw, _secret):
            if not stages:
                self.competing_initializer("authenticating original review")
                stages.append("review")
            return self.authentication.return_value

        def guarded():
            self.assertEqual(self.path.read_bytes(), self.before)
            self.assertEqual(len(list(self.directory.glob(".web-accounts-*.tmp"))), 1)
            self.competing_initializer("staged; about to compare and replace")
            stages.append("staged")
            return True

        self.authentication.side_effect = at_authentication
        with (
            patch.object(accounts, "_locked", wraps=accounts._locked) as exclusive,
            patch.object(accounts, "_unchanged", side_effect=recheck),
            self.hold() as transaction,
        ):
            self.assertEqual(transaction.review._path, self.path)
            self.assert_unchanged(self.before)
            self.competing_initializer("after review; before CAS admission")
            self.commit(transaction, transaction.review, current=guarded)
            self.competing_initializer("after verified CAS; before scope exit")
        exclusive.assert_called_once_with(self.path)
        self.assertEqual(stages, ["review", "staged", "postcheck"])
        self.assertEqual(len(descriptors), 1)
        self.competing_initializer("after scope exit", excluded=False)
        self.assert_unchanged(self.after)
        self.sealing.assert_called_once_with(
            accounts.AccountStore(
                SETUP, (accounts.Account(USERNAME, (WORKSPACE,), REGISTRATION),)
            ),
            PHRASE.encode(),
        )

    def test_standalone_review_releases_lock_and_commit_still_accepts_no_guard(self):
        review = accounts.review_registration(
            self.path, PHRASE, USERNAME, workspace_ids=(WORKSPACE,)
        )
        self.competing_initializer("after standalone review", excluded=False)
        accounts.commit_registration(
            self.path, PHRASE, review, registration=REGISTRATION
        )
        self.competing_initializer("after standalone commit", excluded=False)
        self.assert_unchanged(self.after)

    def test_copied_foreign_closed_and_cross_thread_bindings_refuse(self):
        with self.hold() as expired:
            pass
        with self.hold() as transaction:
            for other, review in (
                (expired, expired.review),
                (replace(transaction), transaction.review),
                (transaction, expired.review),
                (transaction, replace(transaction.review)),
            ):
                with self.assertRaisesRegex(
                    accounts.WebAccountsError, "no longer current"
                ):
                    self.commit(other, review)
            with ThreadPoolExecutor(max_workers=1) as pool:
                pending = pool.submit(self.commit, transaction, transaction.review)
                with self.assertRaisesRegex(
                    accounts.WebAccountsError, "no longer current"
                ):
                    pending.result(timeout=2)
            self.sealing.assert_not_called()
            self.commit(transaction, transaction.review)
        with self.assertRaisesRegex(accounts.WebAccountsError, "no longer current"):
            self.commit(transaction, transaction.review)
        self.assert_unchanged(self.after)

    def test_rejected_checked_cas_spends_attempt_but_retains_flock(self):
        for verdict in (False, 1, None):
            with self.subTest(verdict=verdict), self.hold() as transaction:
                with self.assertRaisesRegex(
                    accounts.WebAccountsError, "no longer current"
                ):
                    self.commit(
                        transaction, transaction.review, current=lambda: verdict
                    )
                with self.assertRaisesRegex(
                    accounts.WebAccountsError, "no longer current"
                ):
                    self.commit(transaction, transaction.review)
                self.competing_initializer("after refusal; before scope exit")
                self.assert_unchanged(self.before)
        self.competing_initializer("after refused scope exit", excluded=False)

    def test_directory_substitution_refuses_before_second_authentication(self):
        displaced = self.root / "displaced"
        with self.hold() as transaction:
            self.directory.rename(displaced)
            self.directory.mkdir(mode=0o700)
            self.write_fixture(self.before)
            with self.assertRaisesRegex(accounts.WebAccountsError, "unsafe"):
                self.commit(transaction, transaction.review)
        self.authentication.assert_called_once()
        self.sealing.assert_not_called()
        self.assertEqual(list(displaced.iterdir()), [displaced / self.path.name])
        self.assertEqual((displaced / self.path.name).read_bytes(), self.before)
        self.assert_unchanged(self.before)

    def test_missing_directory_during_commit_is_a_fixed_refusal_inside_scope(self):
        with self.hold() as transaction:
            self.directory.rename(self.root / "displaced")
            with self.assertRaisesRegex(accounts.WebAccountsError, "unsafe"):
                self.commit(transaction, transaction.review)
        self.authentication.assert_called_once()
        self.sealing.assert_not_called()
        self.assertFalse(self.directory.exists())

    def test_substitution_observed_at_last_guard_does_not_replace_either_file(self):
        displaced = self.root / "displaced"

        def move_directory():
            self.directory.rename(displaced)
            self.directory.mkdir(mode=0o700)
            self.write_fixture(self.before)
            return True

        with self.hold() as transaction:
            with self.assertRaisesRegex(accounts.WebAccountsError, "unsafe"):
                self.commit(transaction, transaction.review, current=move_directory)
        self.assertEqual(list(displaced.iterdir()), [displaced / self.path.name])
        self.assertEqual((displaced / self.path.name).read_bytes(), self.before)
        self.assert_unchanged(self.before)

    def test_publication_uncertainty_spends_attempt_and_is_not_reported_as_refusal(
        self,
    ):
        sync = os.fsync

        def fail_directory_sync(fd):
            if stat.S_ISDIR(os.fstat(fd).st_mode):
                raise OSError("synthetic post-replace failure")
            return sync(fd)

        with self.hold() as transaction:
            with (
                patch.object(accounts.os, "fsync", side_effect=fail_directory_sync),
                self.assertRaisesRegex(
                    accounts.WebAccountsError, "could not be verified"
                ),
            ):
                self.commit(transaction, transaction.review)
            self.assert_unchanged(self.after)
            with self.assertRaisesRegex(accounts.WebAccountsError, "no longer current"):
                self.commit(transaction, transaction.review)
            self.competing_initializer("after unverified CAS; before scope exit")
        self.competing_initializer("after unverified scope exit", excluded=False)


@unittest.skipUnless(HAS_CRYPTO, "optional cryptography not installed")
class EncryptedStoreTests(StoreFixture):
    def test_held_registration_reauthenticates_and_roundtrips_with_standalone_read(
        self,
    ):
        self.create()
        with accounts._hold_registration(
            self.path, PHRASE, USERNAME, workspace_ids=(WORKSPACE,)
        ) as transaction:
            transaction.commit(
                PHRASE,
                transaction.review,
                registration=REGISTRATION,
                current=lambda: True,
            )
        self.assertEqual(
            accounts.load(self.path, PHRASE).accounts,
            (accounts.Account(USERNAME, (WORKSPACE,), REGISTRATION),),
        )

    def test_registration_rechecks_scope_generation_and_current_proof_before_publish(
        self,
    ):
        self.create()
        reviewed = accounts.review_registration(
            self.path, PHRASE, USERNAME, workspace_ids=(WORKSPACE,)
        )
        before = self.path.read_bytes()
        for edits in (
            {"username": "INVALID"},
            {"workspace_ids": [WORKSPACE]},
            {"replacing": 1},
            {"_ciphertext_digest": b"\x00" * 32},
        ):
            with (
                self.subTest(fields=tuple(edits)),
                self.assertRaises(accounts.WebAccountsError),
            ):
                accounts.commit_registration(
                    self.path,
                    PHRASE,
                    replace(reviewed, **edits),
                    registration=REGISTRATION,
                )
            self.assert_unchanged(before)
        with self.assertRaisesRegex(accounts.WebAccountsError, "no longer current"):
            accounts.commit_registration(
                self.path,
                PHRASE,
                reviewed,
                registration=REGISTRATION,
                current=lambda: False,
            )
        self.assert_unchanged(before)

    def test_registration_reports_uncertainty_if_interrupted_after_actual_replace(self):
        self.create()
        reviewed = accounts.review_registration(
            self.path, PHRASE, USERNAME, workspace_ids=(WORKSPACE,)
        )
        replace_file = os.replace

        def publish_and_interrupt(*args, **kwargs):
            replace_file(*args, **kwargs)
            raise KeyboardInterrupt

        with (
            patch.object(accounts.os, "replace", side_effect=publish_and_interrupt),
            self.assertRaisesRegex(accounts.WebAccountsError, "could not be verified"),
        ):
            accounts.commit_registration(
                self.path, PHRASE, reviewed, registration=REGISTRATION
            )
        self.assertEqual(
            accounts.load(self.path, PHRASE).accounts,
            (accounts.Account(USERNAME, (WORKSPACE,), REGISTRATION),),
        )
        self.assertEqual(list(self.directory.iterdir()), [self.path])

    def test_two_writers_from_same_review_cannot_both_publish(self):
        self.create()
        reviewed = accounts.review_registration(
            self.path, PHRASE, USERNAME, workspace_ids=(WORKSPACE,)
        )
        records = [REGISTRATION, b"second synthetic upload"]

        def commit(record):
            try:
                accounts.commit_registration(
                    self.path, PHRASE, reviewed, registration=record
                )
            except accounts.WebAccountsError as err:
                self.assertIn("no longer current", str(err))
                return None
            return record

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(commit, records))
        accepted = [item for item in results if item is not None]
        self.assertEqual(len(accepted), 1)
        self.assertEqual(
            accounts.load(self.path, PHRASE).accounts[0].registration, accepted[0]
        )

    def test_reviewed_registration_enrolls_and_resets_under_same_scope(self):
        self.create()
        before = self.path.read_bytes()
        reviewed = accounts.review_registration(
            self.path, PHRASE, USERNAME, workspace_ids=(WORKSPACE,)
        )
        self.assert_unchanged(before)
        self.assertEqual(reviewed.setup, SETUP)
        self.assertNotIn(USERNAME, repr(reviewed))
        accounts.commit_registration(
            self.path, PHRASE, reviewed, registration=REGISTRATION
        )
        enrolled = self.path.read_bytes()
        with self.assertRaisesRegex(accounts.WebAccountsError, "no longer current"):
            accounts.commit_registration(
                self.path, PHRASE, reviewed, registration=b"another synthetic record"
            )
        with self.assertRaisesRegex(
            accounts.WebAccountsError, "existing workspace scope"
        ):
            accounts.review_registration(
                self.path,
                PHRASE,
                USERNAME,
                workspace_ids=(WORKSPACE, OTHER_WORKSPACE),
                replacing=True,
            )
        self.assert_unchanged(enrolled)
        reset = accounts.review_registration(
            self.path, PHRASE, USERNAME, workspace_ids=(WORKSPACE,), replacing=True
        )
        replacement = b"synthetic reset record"
        accounts.commit_registration(self.path, PHRASE, reset, registration=replacement)
        store = accounts.load(self.path, PHRASE)
        self.assertEqual(store.setup, SETUP)
        self.assertEqual(
            store.accounts, (accounts.Account(USERNAME, (WORKSPACE,), replacement),)
        )

    def test_other_account_change_stales_a_review_without_writing(self):
        self.create()
        reviewed = accounts.review_registration(
            self.path, PHRASE, USERNAME, workspace_ids=(WORKSPACE,)
        )
        self.add("a-different-account", workspace_ids=(OTHER_WORKSPACE,))
        changed = self.path.read_bytes()
        with self.assertRaisesRegex(accounts.WebAccountsError, "no longer current"):
            accounts.commit_registration(
                self.path, PHRASE, reviewed, registration=REGISTRATION
            )
        self.assert_unchanged(changed)

    def test_review_cannot_follow_a_replaced_storage_directory(self):
        self.create()
        reviewed = accounts.review_registration(
            self.path, PHRASE, USERNAME, workspace_ids=(WORKSPACE,)
        )
        displaced = self.root / "displaced"
        self.directory.rename(displaced)
        self.directory.mkdir(mode=0o700)
        raw = (displaced / self.path.name).read_bytes()
        self.write_fixture(raw)
        with self.assertRaisesRegex(accounts.WebAccountsError, "no longer current"):
            accounts.commit_registration(
                self.path, PHRASE, reviewed, registration=REGISTRATION
            )
        self.assertEqual((displaced / self.path.name).read_bytes(), raw)
        self.assert_unchanged(raw)

    def test_roundtrip_at_record_and_scope_limits_and_no_plaintext(self):
        maximum_setup = SETUP.ljust(accounts.MAX_SETUP_BYTES, b"x")
        maximum_record = REGISTRATION.ljust(accounts.MAX_REGISTRATION_BYTES, b"y")
        name = USERNAME.ljust(32, "z")
        scope = [f"{number:032x}" for number in range(accounts.MAX_WORKSPACES, 0, -1)]
        self.create(setup=maximum_setup)
        empty = self.path.read_bytes()
        self.assertEqual(accounts.load(self.path, PHRASE).accounts, ())
        self.add(name, workspace_ids=scope, record=maximum_record)
        scope.reverse()
        self.add("another-account", workspace_ids=(WORKSPACE, OTHER_WORKSPACE))

        loaded = accounts.load(self.path, PHRASE)
        self.assertEqual(loaded.setup, maximum_setup)
        self.assertEqual(
            loaded.accounts,
            (
                accounts.Account(
                    "another-account", (WORKSPACE, OTHER_WORKSPACE), REGISTRATION
                ),
                accounts.Account(name, tuple(scope), maximum_record),
            ),
        )
        raw = self.path.read_bytes()
        self.assertNotEqual(
            raw[: accounts._HEADER_SIZE], empty[: accounts._HEADER_SIZE]
        )
        for secret in (
            PHRASE.encode(),
            name.encode(),
            WORKSPACE.encode(),
            SETUP,
            REGISTRATION,
            base64.b64encode(maximum_setup),
            base64.b64encode(maximum_record),
        ):
            self.assertNotIn(secret, raw)
        for value in (name, WORKSPACE, "synthetic-protocol", PHRASE):
            self.assertNotIn(value, repr(loaded))
            self.assertNotIn(value, repr(loaded.accounts))
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)
        self.assertEqual(self.path.stat().st_nlink, 1)
        self.assert_unchanged(raw)

    def test_duplicate_missing_and_wrong_passphrase_do_not_rewrite_records(self):
        self.create()
        self.add()
        before = self.path.read_bytes()
        for operation, message in (
            (self.create, "not overwrite"),
            (lambda: self.add(workspace_ids=(OTHER_WORKSPACE,)), "already exists"),
            (
                lambda: accounts.remove_account(self.path, PHRASE, "absent"),
                "not present",
            ),
            (lambda: self.add("another", phrase="incorrect"), "incorrect passphrase"),
        ):
            with self.assertRaisesRegex(accounts.WebAccountsError, message):
                operation()
            self.assert_unchanged(before)
        accounts.remove_account(self.path, PHRASE, USERNAME)
        self.assertEqual(
            accounts.load(self.path, PHRASE), accounts.AccountStore(SETUP, ())
        )
        self.assertNotEqual(self.path.read_bytes(), before)

    def test_modified_header_ciphertext_and_truncation_fail_closed(self):
        self.create()
        raw = self.path.read_bytes()
        for index in (
            0,
            len(accounts._FORMAT) - 1,
            len(accounts._FORMAT),
            accounts._HEADER_SIZE - 1,
            len(raw) - 1,
        ):
            with self.subTest(index=index):
                damaged = raw[:index] + bytes([raw[index] ^ 1]) + raw[index + 1 :]
                self.write_fixture(damaged)
                with self.assertRaises(accounts.WebAccountsError):
                    self.add()
                self.assert_unchanged(damaged)
        short = raw[: accounts._HEADER_SIZE]
        self.write_fixture(short)
        with (
            patch.object(accounts, "_cipher", side_effect=AssertionError),
            self.assertRaisesRegex(accounts.WebAccountsError, "invalid or unsupported"),
        ):
            accounts.load(self.path, PHRASE)
        self.assert_unchanged(short)

    def test_real_vault_and_web_account_formats_are_not_interchangeable(self):
        from oc_box import vault

        vault_path = self.directory / "vault.json"
        with vault.locked(vault_path, create=True):
            vault.write_encrypted(vault_path, {"EXAMPLE_TOKEN": "synthetic"}, PHRASE)
        self.create()
        with self.assertRaises(vault.VaultError):
            vault.parse_envelope(self.path.read_bytes())
        with (
            patch.object(accounts, "_cipher", side_effect=AssertionError),
            self.assertRaisesRegex(accounts.WebAccountsError, "invalid or unsupported"),
        ):
            accounts.load(vault_path, PHRASE)

    def write_authenticated_fixture(self, plaintext):
        salt, nonce = os.urandom(16), os.urandom(12)
        prefix = accounts._FORMAT + salt + nonce
        cipher, _ = accounts._cipher(PHRASE.encode(), salt)
        self.write_fixture(prefix + cipher.encrypt(nonce, plaintext, prefix))
        return self.path.read_bytes()

    def test_authentication_does_not_waive_schema_or_scope_validation(self):
        current = {
            "username": USERNAME,
            "workspace_ids": [WORKSPACE],
            "registration": "Zg==",
        }
        for description, plaintext in (
            ("duplicate key", b'{"setup":"Zg==","setup":"Zg==","accounts":[]}'),
            ("deep nesting", b"[" * 1500 + b"]" * 1500),
            (
                "duplicate name",
                json.dumps({"setup": "Zg==", "accounts": [current, current]}),
            ),
            (
                "wildcard",
                json.dumps(
                    {"setup": "Zg==", "accounts": [dict(current, workspace_ids=["*"])]}
                ),
            ),
            ("noncanonical base64", json.dumps({"setup": "Zh==", "accounts": []})),
            (
                "unknown field",
                json.dumps(
                    {
                        "setup": "Zg==",
                        "accounts": [dict(current, password="LEAK_SENTINEL")],
                    }
                ),
            ),
        ):
            with self.subTest(description=description):
                before = self.write_authenticated_fixture(
                    plaintext.encode("ascii")
                    if isinstance(plaintext, str)
                    else plaintext
                )
                with self.assertRaisesRegex(
                    accounts.WebAccountsError, "invalid or unsupported"
                ) as raised:
                    self.add("another")
                self.assertNotIn("LEAK_SENTINEL", str(raised.exception))
                self.assert_unchanged(before)

    def test_account_limit_does_not_evict_or_overwrite(self):
        rows = [
            {
                "username": f"account-{index}",
                "workspace_ids": [WORKSPACE],
                "registration": "Zg==",
            }
            for index in range(accounts.MAX_ACCOUNTS)
        ]
        full = self.write_authenticated_fixture(
            json.dumps({"setup": "Zg==", "accounts": rows}).encode("ascii")
        )
        with self.assertRaisesRegex(accounts.WebAccountsError, "account limit"):
            self.add()
        self.assert_unchanged(full)
        oversized = self.write_authenticated_fixture(
            json.dumps({"setup": "Zg==", "accounts": [*rows, rows[0]]}).encode("ascii")
        )
        with self.assertRaisesRegex(
            accounts.WebAccountsError, "invalid or unsupported"
        ):
            accounts.load(self.path, PHRASE)
        self.assert_unchanged(oversized)

    def test_racing_cooperating_writers_keep_both_records(self):
        self.create()
        ready = threading.Barrier(2, timeout=3)

        def write(name):
            ready.wait()
            self.add(name)

        with ThreadPoolExecutor(2) as executor:
            writers = [executor.submit(write, name) for name in ("first", "second")]
            for writer in writers:
                writer.result(timeout=10)
        self.assertEqual(
            [entry.username for entry in accounts.load(self.path, PHRASE).accounts],
            ["first", "second"],
        )

    def test_detected_inode_substitution_is_not_overwritten(self):
        self.create()
        original = self.path.read_bytes()
        substitute = self.directory / "substitute"
        substitute.touch(mode=0o600)
        substitute.write_bytes(b"external replacement")
        seal = accounts._seal

        def replace_after_sealing(*args):
            result = seal(*args)
            substitute.replace(self.path)
            return result

        with (
            patch.object(accounts, "_seal", side_effect=replace_after_sealing),
            self.assertRaisesRegex(accounts.WebAccountsError, "unsafe"),
        ):
            self.add()
        self.assertNotEqual(original, self.path.read_bytes())
        self.assert_unchanged(b"external replacement")

    def test_detected_parent_substitution_is_not_used_for_publication(self):
        self.create()
        before = self.path.read_bytes()
        detached = self.root / "detached"
        seal = accounts._seal

        def move_parent_after_sealing(*args):
            result = seal(*args)
            self.directory.rename(detached)
            self.directory.mkdir(mode=0o700)
            return result

        with (
            patch.object(accounts, "_seal", side_effect=move_parent_after_sealing),
            self.assertRaisesRegex(accounts.WebAccountsError, "unsafe"),
        ):
            self.add()
        self.assertEqual(list(self.directory.iterdir()), [])
        self.assertEqual(list(detached.iterdir()), [detached / self.path.name])
        self.assertEqual((detached / self.path.name).read_bytes(), before)

    def test_unexpected_destination_at_first_publication_is_never_replaced(self):
        link = os.link

        def conflicting_link(*args, **kwargs):
            self.write_fixture(b"external earlier file")
            return link(*args, **kwargs)

        with (
            patch.object(accounts.os, "link", side_effect=conflicting_link),
            self.assertRaisesRegex(accounts.WebAccountsError, "not overwrite"),
        ):
            self.create()
        self.assert_unchanged(b"external earlier file")

    def test_prepublication_io_failure_and_postpublication_uncertainty(self):
        self.create()
        before = self.path.read_bytes()
        with (
            patch.object(accounts.os, "replace", side_effect=OSError("private path")),
            self.assertRaisesRegex(accounts.WebAccountsError, "unsafe") as raised,
        ):
            self.add()
        self.assertNotIn("private path", str(raised.exception))
        self.assert_unchanged(before)

        fsync = os.fsync

        def fail_directory_sync(fd):
            if stat.S_ISDIR(os.fstat(fd).st_mode):
                raise OSError("private path")
            return fsync(fd)

        with (
            patch.object(accounts.os, "fsync", side_effect=fail_directory_sync),
            self.assertRaisesRegex(accounts.WebAccountsError, "reload before retrying"),
        ):
            self.add()
        self.assertNotEqual(self.path.read_bytes(), before)
        self.assertEqual(
            accounts.load(self.path, PHRASE).accounts[0].username, USERNAME
        )
