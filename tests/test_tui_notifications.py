"""Pure notification policy tests; no optional UI or domain dependencies."""

from dataclasses import FrozenInstanceError
import errno
import unittest

from oc_box.tui_notifications import FailureNotice, failure_notice


class SecretError(Exception):
    def __str__(self):
        raise AssertionError("Exception text must never be requested")

    def __repr__(self):
        raise AssertionError("Exception repr must never be requested")


class ReasonError(SecretError):
    def __init__(self, reason):
        self.reason = reason


class FailureNoticeTests(unittest.TestCase):
    def test_catalog_and_ui_preflight_reason_matrix_has_actionable_fixed_copy(self):
        cases = {
            "read-only": "read-only",
            "demo": "Synthetic demo",
            "selection-required": "Select a session",
            "busy": "Another action",
            "session-active": "active session or job",
            "session-terminal-running": "running persistent shell",
            "managed-archive-has-children": "managed host archive has native child",
            "ownership-unknown": "Ownership could not be verified",
            "invalid-activity": "activity could not be validated",
            "invalid-jobs": "background-job activity",
            "invalid-terminals": "persistent-shell status",
            "origin-changed": "source changed or was disconnected",
            "identity-changed": "native identity changed",
            "snapshot-changed": "changed after review",
            "snapshot-required": "current session snapshot",
            "invalid-preview": "preview is missing, expired",
            "review-limit": "50-session review limit",
            "confirmation-required": "Type DELETE exactly",
            "invalid-archive-state": "archive state is invalid",
            "session-missing": "no longer in the verified inventory",
            "invalid-identity": "identity could not be validated",
            "invalid-directory": "directory could not be validated",
            "invalid-session": "session metadata could not be validated",
            "invalid-session-tree": "parent/child tree could not be verified",
            "invalid-inventory": "incomplete or malformed",
            "duplicate-session": "duplicate session identities",
            "inconsistent-directory": "does not match the selected directory",
            "invalid-cursor": "pagination was incomplete or inconsistent",
            "inventory-limit": "exceeded dashboard safety bounds",
            "invalid-provenance": "provenance could not be verified",
        }
        for reason, phrase in cases.items():
            with self.subTest(reason=reason):
                notice = failure_notice("delete", ReasonError(reason), dispatched=True)
                self.assertEqual(notice.title, "Deletion refused")
                self.assertIn(phrase, notice.message)
                self.assertEqual(notice.severity, "warning")
                self.assertEqual(notice.timeout, 8.0)
                self.assertNotIn("nothing changed", notice.message.lower())

    def test_storage_purge_backup_and_private_metadata_reason_matrix(self):
        cases = {
            "permission-denied": "Access was denied",
            "file-missing": "file is no longer available",
            "state-changed": "state changed after review",
            "storage-full": "space or quota is exhausted",
            "read-only-storage": "mounted read-only",
            "unsafe-state": "filesystem state could not be safely validated",
            "directory-not-empty": "directory is not empty",
            "workspace-running": "active owner",
            "workspace-attached": "clients are still attached",
            "workspace-unknown": "registration is unavailable",
            "unsafe-backup-file": "backup file could not be safely validated",
            "outside-backup-root": "outside the configured backup location",
            "invalid-backup": "backup could not be validated",
            "connect-target-in-sessions-first": "Open Sessions",
            "session-workspace-mismatch": "different workspace",
            "host-session-backups-only": "individual native session history",
            "workspace-selection-required": "Select a registered workspace",
            "selection-changed": "target changed after review",
            "output-exists": "backup output already exists",
            "destination-exists": "restore destination already exists",
            "path-required": "required file or destination path is missing",
            "private-owned-file-required": "private and owned by the current user",
            "regular-file-required": "regular, singly linked file",
            "file-unavailable": "file could not be opened safely",
            "symlink-path": "symbolic link",
            "invalid-confirmation": "typed confirmation does not match",
            "workspace-validation": "physical identity could not be validated",
            "trusted-path-exposure": "trusted application or state files",
            "unsafe-favorites-state": "favorite metadata could not be safely validated",
            "favorites-busy": "favorite metadata is busy",
            "favorites-state-failed": "favorite metadata could not be read or saved",
            "invalid-favorites-state": "favorite metadata is invalid",
            "favorites-limit": "favorite metadata limit was reached",
        }
        for reason, phrase in cases.items():
            with self.subTest(reason=reason):
                notice = failure_notice("storage", reason=reason)
                self.assertEqual(notice.title, "Storage action refused")
                self.assertIn(phrase, notice.message)

    def test_errno_matrix_never_formats_os_errors_or_filenames(self):
        cases = {
            errno.EACCES: "Access was denied",
            errno.EPERM: "Access was denied",
            errno.ENOENT: "file is no longer available",
            errno.ESTALE: "state changed",
            errno.EBUSY: "Another action",
            errno.EAGAIN: "Another action",
            errno.EEXIST: "state changed",
            errno.ENOSPC: "space or quota",
            errno.EDQUOT: "space or quota",
            errno.EROFS: "mounted read-only",
            errno.ENOTEMPTY: "directory is not empty",
            errno.ENOTDIR: "filesystem state",
            errno.EISDIR: "filesystem state",
            errno.ELOOP: "filesystem state",
        }
        for number, phrase in cases.items():
            with self.subTest(number=number):
                error = OSError(number, "PRIVATE_ERROR", "/private/secret-file")
                notice = failure_notice("backup-file-delete", error)
                self.assertIn(phrase, notice.message)
                self.assertNotIn("PRIVATE_ERROR", repr(notice))
                self.assertNotIn("/private/secret-file", repr(notice))
                dispatched = failure_notice("workspace-purge", error, dispatched=True)
                self.assertEqual(dispatched.title, "Workspace purge outcome unverified")
                self.assertIn("before any retry", dispatched.message)

    def test_unknown_secret_errors_and_reason_properties_are_never_rendered(self):
        class BadReason(SecretError):
            @property
            def reason(self):
                raise RuntimeError("PRIVATE_PROPERTY")

        class BadErrno(OSError):
            @property
            def errno(self):
                raise RuntimeError("PRIVATE_ERRNO")

        errors = (
            SecretError("PRIVATE_ARGUMENT"),
            ReasonError("PRIVATE_REASON"),
            ReasonError(SecretError()),
            BadReason(),
            BadErrno(),
            None,
        )
        for error in errors:
            notice = failure_notice("PRIVATE_OPERATION", error)
            self.assertEqual(notice.title, "Action refused")
            self.assertIn("could not be safely verified", notice.message)
            self.assertNotIn("PRIVATE", repr(notice))
            ambiguous = failure_notice("delete", error, dispatched=True)
            self.assertEqual(ambiguous.title, "Deletion outcome unverified")
            self.assertIn("before any retry", ambiguous.message)
            self.assertNotIn("PRIVATE", repr(ambiguous))
        notice = failure_notice(
            SecretError(), SecretError(), reason=SecretError(), count=SecretError()
        )
        self.assertEqual(notice.title, "Action refused")

    def test_transport_and_invalid_acknowledgement_are_conservative_after_dispatch(
        self,
    ):
        for reason in (
            "request-failed",
            "invalid-mutation-response",
            "UNKNOWN_PRIVATE_REASON",
        ):
            with self.subTest(reason=reason):
                notice = failure_notice("delete", ReasonError(reason), dispatched=True)
                self.assertEqual(notice.title, "Deletion outcome unverified")
                self.assertEqual(notice.severity, "error")
                self.assertIn("may already have changed", notice.message)
                self.assertIn("Inspect and refresh", notice.message)
                self.assertNotIn("nothing changed", notice.message.lower())
                self.assertNotIn("UNKNOWN_PRIVATE_REASON", notice.message)
        before = failure_notice("delete", reason="request-failed")
        self.assertEqual(before.title, "Deletion refused")
        self.assertIn("Native verification", before.message)
        self.assertEqual(
            failure_notice("delete", reason="invalid-mutation-response").title,
            "Deletion outcome unverified",
        )

    def test_completed_mutation_wins_over_refresh_failure_reason(self):
        for operation, title in (
            ("delete", "Deletion"),
            ("archive", "Archive"),
            ("unarchive", "Restore"),
            ("storage", "Storage action"),
            ("workspace-purge", "Workspace purge"),
            ("backup-file-delete", "Backup file deletion"),
            ("PRIVATE_UNKNOWN", "Action"),
        ):
            with self.subTest(operation=operation):
                notice = failure_notice(
                    operation, SecretError(), completed=True, dispatched=True
                )
                self.assertEqual(
                    notice.title, title + " completed; refresh unavailable"
                )
                self.assertIn("Displayed rows may be stale", notice.message)
                self.assertIn("do not repeat the completed action", notice.message)
                self.assertEqual(notice.severity, "warning")
                self.assertNotIn("PRIVATE", repr(notice))

    def test_operation_context_is_allowlisted_for_refusal_and_ambiguity(self):
        for operation, title in (
            ("delete", "Deletion"),
            ("archive", "Archive"),
            ("unarchive", "Restore"),
            ("storage", "Storage action"),
            ("workspace-purge", "Workspace purge"),
            ("backup-file-delete", "Backup file deletion"),
            ("mutation", "Action"),
        ):
            with self.subTest(operation=operation):
                self.assertEqual(
                    failure_notice(operation, reason="busy").title, title + " refused"
                )
                self.assertEqual(
                    failure_notice(operation, dispatched=True).title,
                    title + " outcome unverified",
                )
        for operation in ("storage", "workspace-purge", "backup-file-delete"):
            self.assertNotIn(
                "Select a session",
                failure_notice(operation, reason="selection-required").message,
            )

    def test_explicit_aliases_are_fixed_and_unknown_aliases_do_not_echo(self):
        for alias, canonical in (
            ("readonly", "read-only"),
            ("no-selection", "selection-required"),
            ("operation-busy", "busy"),
            ("source-changed", "origin-changed"),
            ("source-disconnected", "origin-changed"),
            ("wrong-confirmation", "invalid-confirmation"),
            ("review-limit-50", "review-limit"),
            ("backup-not-found", "file-missing"),
            ("backup-changed", "state-changed"),
            ("owner-active", "workspace-running"),
        ):
            with self.subTest(alias=alias):
                self.assertEqual(
                    failure_notice("delete", reason=alias),
                    failure_notice("delete", reason=canonical),
                )
        self.assertNotIn(
            "PRIVATE", failure_notice("delete", reason="PRIVATE_UNKNOWN").message
        )

    def test_management_confirmation_copy_never_assumes_delete_is_the_typed_phrase(
        self,
    ):
        for reason in (
            "invalid-confirmation",
            "confirmation-mismatch",
            "wrong-confirmation",
        ):
            with self.subTest(reason=reason):
                notice = failure_notice("workspace-purge", reason=reason)
                self.assertEqual(notice.title, "Workspace purge refused")
                self.assertIn("required phrase in the open review", notice.message)
                self.assertNotIn("Type DELETE", notice.message)

    def test_only_bounded_plain_integer_counts_can_appear_in_review_notice(self):
        self.assertIn(
            "contains 51 sessions",
            failure_notice("delete", reason="review-limit", count=51).message,
        )
        for count in (True, -1, 1000000001, "PRIVATE_COUNT", SecretError()):
            notice = failure_notice("delete", reason="review-limit", count=count)
            self.assertIn("50-session review limit", notice.message)
            self.assertNotIn("PRIVATE_COUNT", notice.message)

    def test_cancel_notice_is_informational_and_notice_is_frozen(self):
        notice = failure_notice("delete", reason="cancelled")
        self.assertEqual(notice.title, "Deletion cancelled")
        self.assertEqual(notice.severity, "information")
        self.assertIsInstance(notice, FailureNotice)
        with self.assertRaises(FrozenInstanceError):
            notice.title = "replaced"


if __name__ == "__main__":
    unittest.main()
