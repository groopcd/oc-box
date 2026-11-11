import builtins
import contextlib
import ctypes
import errno
import os
from pathlib import Path
import stat
import subprocess
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from oc_box import private_memory as memory


@contextlib.contextmanager
def without_os_attributes(*names):
    with patch.dict(os.__dict__):
        for name in names:
            os.__dict__.pop(name, None)
        yield


class PrivateMemoryTests(unittest.TestCase):
    def test_module_import_does_not_load_ctypes(self):
        subprocess.run(
            [
                sys.executable,
                "-B",
                "-S",
                "-c",
                "import sys\n"
                "sys.path.insert(0, sys.argv[1])\n"
                "from oc_box import private_memory\n"
                "assert 'ctypes' not in sys.modules\n",
                str(Path(memory.__file__).resolve().parents[1]),
            ],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=10,
        )

    def test_native_binding_is_preferred_without_importing_ctypes(self):
        original_import = builtins.__import__

        def guarded_import(name, *args, **kwargs):
            if name == "ctypes":
                self.fail("native creation must not import ctypes")
            return original_import(name, *args, **kwargs)

        for case, flags_present in (("native-flags", True), ("constant-flags", False)):
            with self.subTest(case=case), contextlib.ExitStack() as stack:
                stack.enter_context(patch.object(memory.sys, "platform", "linux"))
                if flags_present:
                    stack.enter_context(patch.object(os, "MFD_CLOEXEC", 1, create=True))
                else:
                    stack.enter_context(without_os_attributes("MFD_CLOEXEC"))
                native = stack.enter_context(
                    patch.object(os, "memfd_create", return_value=73, create=True)
                )
                loader = stack.enter_context(patch.object(ctypes, "CDLL"))
                stack.enter_context(
                    patch("builtins.__import__", side_effect=guarded_import)
                )
                self.assertEqual(memory.create("synthetic-native"), 73)
                native.assert_called_once_with("synthetic-native", 1)
                loader.assert_not_called()

    def test_native_errors_are_propagated_without_libc_retry(self):
        for case, code in (("unimplemented", errno.ENOSYS), ("denied", errno.EPERM)):
            with self.subTest(case=case):
                failure = OSError(code, "synthetic native failure")
                with (
                    patch.object(memory.sys, "platform", "linux"),
                    patch.object(os, "memfd_create", side_effect=failure, create=True),
                    patch.object(ctypes, "CDLL") as loader,
                ):
                    with self.assertRaises(OSError) as caught:
                        memory.create("synthetic-native-failure")
                self.assertIs(caught.exception, failure)
                loader.assert_not_called()

    def test_libc_signature_errno_and_atomic_cloexec(self):
        cases = (
            ("unimplemented", errno.ENOSYS),
            ("denied", errno.EPERM),
            ("no-memory", errno.ENOMEM),
            ("missing-errno", 0),
        )
        for case, code in cases:
            with self.subTest(case=case):

                def fail(name, flags):
                    if code:
                        ctypes.set_errno(code)
                    return -1

                function = Mock(side_effect=fail)
                with (
                    patch.object(memory.sys, "platform", "linux"),
                    without_os_attributes("memfd_create", "MFD_CLOEXEC"),
                    patch.object(
                        ctypes,
                        "CDLL",
                        return_value=SimpleNamespace(memfd_create=function),
                    ) as loader,
                ):
                    # A failed libc call that leaves errno unset must not reuse
                    # an unrelated earlier ctypes errno.
                    ctypes.set_errno(errno.EBUSY)
                    with self.assertRaises(OSError) as caught:
                        memory.create("synthetic-libc-failure")
                self.assertEqual(caught.exception.errno, code or errno.EIO)
                loader.assert_called_once_with(None, use_errno=True)
                self.assertEqual(function.argtypes, [ctypes.c_char_p, ctypes.c_uint])
                self.assertIs(function.restype, ctypes.c_int)
                function.assert_called_once_with(b"synthetic-libc-failure", 1)

    def test_libc_loader_symbol_and_call_failures_are_generic_enosys(self):
        cases = (
            ("load-failure", OSError("synthetic private loader detail"), None),
            ("missing-symbol", None, SimpleNamespace()),
            (
                "ffi-failure",
                None,
                SimpleNamespace(
                    memfd_create=Mock(
                        side_effect=RuntimeError("synthetic private loader detail")
                    )
                ),
            ),
        )
        for case, failure, library in cases:
            with (
                self.subTest(case=case),
                patch.object(memory.sys, "platform", "linux"),
                without_os_attributes("memfd_create"),
                patch.object(ctypes, "CDLL", side_effect=failure, return_value=library),
            ):
                with self.assertRaises(OSError) as caught:
                    memory.create("synthetic-unavailable")
                self.assertEqual(caught.exception.errno, errno.ENOSYS)
                self.assertNotIn(
                    "synthetic private loader detail", str(caught.exception)
                )
                self.assertTrue(caught.exception.__suppress_context__)

    def test_missing_ctypes_is_generic_enosys(self):
        with (
            patch.object(memory.sys, "platform", "linux"),
            without_os_attributes("memfd_create"),
            patch.dict(sys.modules, {"ctypes": None}),
        ):
            with self.assertRaises(OSError) as caught:
                memory.create("synthetic-no-ctypes")
        self.assertEqual(caught.exception.errno, errno.ENOSYS)

    def test_fallback_rejects_embedded_nul_without_truncating_the_name(self):
        with (
            patch.object(memory.sys, "platform", "linux"),
            without_os_attributes("memfd_create"),
            patch.object(ctypes, "CDLL") as loader,
        ):
            with self.assertRaises(OSError) as caught:
                memory.create("synthetic\0truncated")
        self.assertEqual(caught.exception.errno, errno.EINVAL)
        loader.assert_not_called()

    def test_non_linux_refuses_both_creation_paths(self):
        for platform in ("darwin", "win32", "freebsd14"):
            with (
                self.subTest(platform=platform),
                patch.object(memory.sys, "platform", platform),
                patch.object(os, "memfd_create", create=True) as native,
                patch.object(ctypes, "CDLL") as loader,
            ):
                with self.assertRaises(OSError) as caught:
                    memory.create("synthetic-non-linux")
                self.assertEqual(caught.exception.errno, errno.ENOSYS)
                native.assert_not_called()
                loader.assert_not_called()

    def test_check_always_closes_created_descriptor(self):
        cases = (
            ("valid", stat.S_IFREG, False, None, None),
            ("not-regular", stat.S_IFIFO, False, None, errno.ENOSYS),
            ("inheritable", stat.S_IFREG, True, None, errno.ENOSYS),
            ("stat-failure", stat.S_IFREG, False, errno.EIO, errno.EIO),
            ("inheritance-query-failure", stat.S_IFREG, None, None, errno.EBADF),
        )
        for case, mode, inheritable, stat_error, expected_errno in cases:
            with (
                self.subTest(case=case),
                patch.object(memory, "create", return_value=73) as create,
                patch.object(
                    os,
                    "fstat",
                    return_value=SimpleNamespace(st_mode=mode | 0o600),
                    side_effect=(
                        OSError(stat_error, "synthetic stat failure")
                        if stat_error
                        else None
                    ),
                ),
                patch.object(
                    os,
                    "get_inheritable",
                    return_value=inheritable,
                    side_effect=(
                        OSError(errno.EBADF, "synthetic query failure")
                        if inheritable is None
                        else None
                    ),
                ),
                patch.object(os, "close") as close,
            ):
                if expected_errno is None:
                    self.assertIsNone(memory.check())
                else:
                    with self.assertRaises(OSError) as caught:
                        memory.check()
                    self.assertEqual(caught.exception.errno, expected_errno)
                create.assert_called_once_with("oc-box-private-output-probe")
                close.assert_called_once_with(73)

    def test_failed_probe_creation_does_not_close_an_unowned_descriptor(self):
        failure = OSError(errno.EPERM, "synthetic probe refusal")
        with (
            patch.object(memory, "create", side_effect=failure),
            patch.object(os, "close") as close,
        ):
            with self.assertRaises(OSError) as caught:
                memory.check()
        self.assertIs(caught.exception, failure)
        close.assert_not_called()

    def test_actionable_diagnostic_identifies_interpreter_and_auth_requirement(self):
        with patch.object(memory.sys, "executable", "/synthetic/python/bin/python3"):
            message = memory.error_message()
        for fragment in (
            "/synthetic/python/bin/python3",
            "private OpenCode credential capture",
            "--no-vault",
            "native saved/OAuth auth",
            "oc-box doctor --feature private-output",
            "compatible host Python",
            "uv tool install --force --python /usr/bin/python3 oc-box",
        ):
            with self.subTest(fragment=fragment):
                self.assertIn(fragment, message)


@unittest.skipUnless(sys.platform == "linux", "Linux anonymous-memory files required")
class LinuxPrivateMemoryTests(unittest.TestCase):
    def test_real_native_and_libc_files_are_binary_regular_and_non_inheritable(self):
        for case in ("native", "libc", "libc-without-python-flag"):
            with self.subTest(case=case), contextlib.ExitStack() as stack:
                if case == "native" and not hasattr(os, "memfd_create"):
                    continue
                if case != "native":
                    names = ["memfd_create"]
                    if case == "libc-without-python-flag":
                        names.append("MFD_CLOEXEC")
                    stack.enter_context(without_os_attributes(*names))
                fd = memory.create("oc-box-synthetic-binary")
                try:
                    self.assertTrue(stat.S_ISREG(os.fstat(fd).st_mode))
                    self.assertIn("memfd:", os.readlink(f"/proc/self/fd/{fd}"))
                    self.assertFalse(os.get_inheritable(fd))
                    value = b"synthetic-private-output\0\xff\x80\n"
                    self.assertEqual(os.write(fd, value), len(value))
                    self.assertEqual(os.lseek(fd, 0, os.SEEK_SET), 0)
                    self.assertEqual(os.read(fd, len(value) + 1), value)
                finally:
                    os.close(fd)

    def test_real_fallback_probe_closes_its_descriptor(self):
        descriptors = []
        original_create = memory.create

        def capture(name):
            fd = original_create(name)
            descriptors.append(fd)
            return fd

        with (
            without_os_attributes("memfd_create", "MFD_CLOEXEC"),
            patch.object(memory, "create", side_effect=capture),
        ):
            self.assertIsNone(memory.check())
        self.assertEqual(len(descriptors), 1)
        with self.assertRaises(OSError) as caught:
            os.fstat(descriptors[0])
        self.assertEqual(caught.exception.errno, errno.EBADF)


if __name__ == "__main__":
    unittest.main()
