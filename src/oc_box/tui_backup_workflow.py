"""Optional dashboard backup presentation, with private non-Input secret editing."""

import json
import signal
import time

from rich.text import Text
from textual import constants, events, work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widget import Widget
from textual.widgets import Button, ProgressBar, Static

if __package__:
    from . import secret_prompt, tui_operation_io
else:
    import secret_prompt
    import tui_operation_io


def private_ui_allowed(app):
    """Textual logs input events before widget handlers in developer modes."""
    if (
        constants.LOG_FILE
        or constants.DEBUG
        or getattr(app, "devtools", None) is not None
    ):
        raise tui_operation_io.OperationIOError()
    tui_operation_io.disable_core_dumps()


class SecretEditor(Widget, can_focus=True):
    """Only masks render; no Input.value, reactive secret, selection or messages."""

    PRIVATE_INPUT = True
    ALLOW_SELECT = False
    DEFAULT_CSS = "SecretEditor { height: 3; border: round $accent; padding: 0 1; }"

    def __init__(self):
        tui_operation_io.disable_core_dumps()
        super().__init__(id="private-secret-editor")
        self._private_editor = secret_prompt.Editor(1024, False)

    def render(self):
        editor = self._private_editor
        # Never pass actual characters to Rich/Textual rendering, even transiently.
        return Text(
            "*" * min(len(editor.chars), max(1, self.size.width - 18))
            + f"  [{len(editor.chars)} chars]"
        )

    def get_selection(self, selection):
        return "", "\n"

    def clear_private(self):
        self._private_editor.erase(0, len(self._private_editor.chars))
        self.refresh()

    def on_key(self, event: events.Key):
        event.stop()
        event.prevent_default()
        try:
            self._private_key(event)
        except Exception:
            self._refuse_input()

    def _private_key(self, event):
        if self.screen._submitted or not self.has_focus:
            return
        if event.key == "enter":
            self.screen.submit()
            return
        if event.key in {"escape", "ctrl+c", "ctrl+d"}:
            self.screen.cancel()
            return
        keys = {
            "left": "left",
            "right": "right",
            "home": "home",
            "end": "end",
            "backspace": "backspace",
            "delete": "delete",
            "ctrl+u": "clear",
            "ctrl+w": "word",
            "ctrl+k": "tail",
            "ctrl+a": "home",
            "ctrl+e": "end",
        }
        if event.key in keys:
            self._private_editor.key(keys[event.key])
        elif event.is_printable and event.character:
            self._private_editor.insert(event.character)
        self._changed()

    def on_paste(self, event: events.Paste):
        event.stop()
        event.prevent_default()
        try:
            if self.screen._submitted or not self.has_focus:
                return
            # Explicit terminal paste, never the app clipboard cache.
            self._private_editor.insert(event.text)
            self._changed()
        except Exception:
            self._refuse_input()

    def _refuse_input(self):
        # Never let a framework traceback render event or editor locals.
        self._private_editor.erase(0, len(self._private_editor.chars))
        try:
            self.screen.cancel()
        except Exception:
            pass

    def _changed(self):
        self.refresh()
        notice = self._private_editor.notice
        self.screen.query_one("#private-secret-status", Static).update(
            notice or "Masked; no copy, reveal, or saved password."
        )

    def on_unmount(self):
        self.clear_private()


class PrivateSecretScreen(ModalScreen):
    PRIVATE_INPUT = True
    ALLOW_SELECT = False
    BINDINGS = [
        Binding("enter", "submit_focused_secret", show=False, priority=True),
        Binding("escape,ctrl+c", "cancel_private_input", show=False, priority=True),
    ]
    CSS = """
    PrivateSecretScreen { align: center middle; }
    PrivateSecretScreen > Vertical { width: 72; max-width: 95%; height: auto;
      padding: 1 2; border: thick $accent; background: $surface; }
    PrivateSecretScreen Static { height: auto; margin-bottom: 1; }
    PrivateSecretScreen Horizontal { height: 3; margin-top: 1; }
    """

    def __init__(self, transport, event):
        tui_operation_io.disable_core_dumps()
        super().__init__()
        self.transport, self.prompt_id = transport, event["id"]
        self.purpose = event["purpose"]
        self._submitted = False

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static(
                {
                    "backup-new": "Choose a backup password (host creation policy applies).",
                    "backup-confirm": "Enter the same backup password again.",
                    "backup-unlock": "Enter this archive's backup password.",
                }[self.purpose],
                markup=False,
            )
            yield Static(
                "Keep this password safely: it is not saved and cannot be recovered. "
                "Terminal recordings can observe input. No key file needs copying.",
                markup=False,
            )
            yield SecretEditor()
            yield Static(
                "Masked; no copy, reveal, or saved password.",
                id="private-secret-status",
                markup=False,
            )
            with Horizontal():
                yield Button("Cancel operation", id="private-secret-cancel")
                yield Button("Continue", id="private-secret-submit", variant="primary")

    def on_mount(self):
        try:
            private_ui_allowed(self.app)
        except Exception:
            self.query_one(SecretEditor).disabled = True
            self.transport.cancel()
            self.query_one("#private-secret-status", Static).update(
                "Private input requires dashboard debug, developer tools, and event logging disabled. Cancelling…"
            )
            return
        for widget in self.walk_children():
            widget.ALLOW_SELECT = False
        if self.purpose == "backup-confirm":
            # A fresh confirmation requires a deliberate focus gesture. Neither
            # queued first-password keys nor repeated Enter can prefill/submit it.
            self.query_one("#private-secret-cancel", Button).focus()
            self.query_one("#private-secret-status", Static).update(
                "Click the password box or Shift+Tab to begin confirmation; queued input is ignored."
            )
        else:
            self.query_one(SecretEditor).focus()

    def action_submit_focused_secret(self):
        if isinstance(self.app.focused, SecretEditor):
            self.submit()

    def action_cancel_private_input(self):
        self.cancel()

    def on_paste(self, event: events.Paste):
        if not isinstance(self.app.focused, SecretEditor):
            event.stop()
            event.prevent_default()

    def submit(self):
        if self._submitted:
            return
        editor = self.query_one(SecretEditor)
        if editor._private_editor.notice:
            return
        content = bytearray("".join(editor._private_editor.chars).encode())
        editor.clear_private()
        self._submitted = True
        self.query_one("#private-secret-submit", Button).disabled = True
        try:
            self.transport.submit_secret(self.prompt_id, content)
            self.query_one("#private-secret-status", Static).update(
                "Submitted to the private child terminal; waiting…"
            )
        except Exception:
            self.query_one("#private-secret-status", Static).update(
                "Private input could not be submitted. Waiting for cancellation cleanup…"
            )
            self.transport.cancel()
        finally:
            content[:] = b"\0" * len(content)
            content.clear()

    def cancel(self):
        self.query_one(SecretEditor).clear_private()
        accepted = self.transport.cancel()
        self.query_one("#private-secret-status", Static).update(
            "Completion already verified; waiting for the result display…"
            if accepted is False
            else "Cancellation requested; waiting for operation cleanup…"
        )
        self.query_one("#private-secret-submit", Button).disabled = True
        self._submitted = True

    def on_button_pressed(self, event):
        event.stop()
        if event.button.id == "private-secret-submit":
            self.submit()
        elif event.button.id == "private-secret-cancel":
            self.cancel()

    def on_unmount(self):
        for editor in self.query(SecretEditor):
            editor.clear_private()


class MetadataReview(ModalScreen):
    BINDINGS = [
        Binding("enter", "ignore_enter", show=False, priority=True),
        Binding("space", "press_focused_review", show=False, priority=True),
    ]
    CSS = """
    MetadataReview { align: center middle; }
    MetadataReview > Vertical { width: 86; max-width: 95%; height: 85%;
      padding: 1 2; border: thick $warning; background: $surface; }
    MetadataReview VerticalScroll { height: 1fr; }
    MetadataReview Horizontal { height: 3; }
    """

    def __init__(self, metadata):
        super().__init__()
        self.metadata = metadata
        self._decision_sent = False

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static(
                "Review authenticated archive metadata before restoring.", markup=False
            )
            with VerticalScroll():
                yield Static(
                    json.dumps(self.metadata, indent=2, ensure_ascii=True), markup=False
                )
                yield Static(
                    "Restore uses fresh IDs and never overwrites existing data or starts a workspace. "
                    "Archived device/Podman privileges are not activated. Native session activity "
                    "checks are process-local and cannot exclude other services.",
                    markup=False,
                )
            with Horizontal():
                yield Button("Cancel restore", id="metadata-cancel")
                yield Button(
                    "Restore reviewed data", id="metadata-approve", variant="primary"
                )

    def on_button_pressed(self, event):
        if event.button.id in {"metadata-cancel", "metadata-approve"}:
            event.stop()
            if self._decision_sent:
                return
            self._decision_sent = True
            self.dismiss(event.button.id == "metadata-approve")

    def action_cancel_private_input(self):
        if self.is_mounted and not self._decision_sent:
            self._decision_sent = True
            self.dismiss(False)

    def on_mount(self):
        self.query_one("#metadata-cancel", Button).focus()

    def on_key(self, event: events.Key):
        # A queued/repeated password Enter can never approve the NEXT review.
        # Restore approval is an explicit click or Space on the focused button.
        if event.key == "enter":
            event.stop()
            event.prevent_default()

    def action_ignore_enter(self):
        pass

    def action_press_focused_review(self):
        button = self.app.focused
        if isinstance(button, Button) and button.id in {
            "metadata-cancel",
            "metadata-approve",
        }:
            button.press()


class BackupWorkflow(ModalScreen):
    BINDINGS = [Binding("ctrl+c", "cancel_operation", show=False, priority=True)]
    CSS = """
    BackupWorkflow { align: center middle; }
    BackupWorkflow > Vertical { width: 88; max-width: 95%; height: 85%;
      padding: 1 2; border: thick $accent; background: $surface; }
    #backup-operation-results { height: 1fr; }
    #backup-operation-stage, #backup-operation-elapsed { height: auto; }
    BackupWorkflow Horizontal { height: 3; }
    """

    def __init__(self, runtime, request):
        super().__init__()
        self.transport = tui_operation_io.OperationProcess(runtime, request)
        self.result = None
        self.finished = False
        self.started = time.monotonic()
        self._details = []

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static(
                "Preparing private backup operation…",
                id="backup-operation-stage",
                markup=False,
            )
            yield Static(
                "0 seconds elapsed · progress is indeterminate",
                id="backup-operation-elapsed",
                markup=False,
            )
            yield ProgressBar(
                total=None,
                show_eta=False,
                show_percentage=False,
                id="backup-operation-progress",
            )
            with VerticalScroll(id="backup-operation-results"):
                yield Static(
                    "Cancellation waits for cleanup. A backup can leave its source container stopped. "
                    "No automatic retry, workspace start, or session connection will run.",
                    id="backup-operation-summary",
                    markup=False,
                )
            with Horizontal():
                yield Button("Cancel operation", id="backup-operation-cancel")
                yield Button("Close", id="backup-operation-close", disabled=True)

    def on_mount(self):
        self.app._private_interaction = True
        self._previous_interrupt = signal.getsignal(signal.SIGINT)
        self._interrupt_handler = lambda *_: self.transport.cancel()
        signal.signal(signal.SIGINT, self._interrupt_handler)
        self._clock = self.set_interval(0.5, self._tick)
        self._run()

    def _tick(self):
        if self.transport._cancelled and not self.finished:
            self.query_one("#backup-operation-cancel", Button).disabled = True
            self.query_one("#backup-operation-stage", Static).update(
                "Cancellation requested; waiting for operation cleanup…"
            )
        self.query_one("#backup-operation-elapsed", Static).update(
            f"{int(time.monotonic() - self.started)} seconds elapsed · progress is indeterminate"
        )

    @work(thread=True, group="backup-operation", exit_on_error=False)
    def _run(self):
        result = None
        try:
            self.transport.start()
            result = self.transport.run(self._from_worker)
        except Exception:
            pass  # fixed failure only; never render/log exception or private locals
        finally:
            if self.app.is_running:
                self.app.call_from_thread(self._finished, result)

    def _from_worker(self, event):
        if not self.app.is_running:
            self.transport.cancel()
            return
        self.app.call_from_thread(self._apply, event)

    def _apply(self, event):
        kind = event["kind"]
        if kind == "stage":
            self.query_one("#backup-operation-stage", Static).update(
                tui_operation_io.STAGE_LABELS[event["stage"]]
            )
        elif kind == "prompt":
            self.app.push_screen(PrivateSecretScreen(self.transport, event))
        elif kind == "prompt-closed":
            screen = self.app.screen
            if (
                isinstance(screen, PrivateSecretScreen)
                and screen.transport is self.transport
            ):
                screen.dismiss()
        elif kind == "review":

            def finish(approved):
                try:
                    self.transport.approve(event["id"], approved is True)
                except Exception:
                    self.transport.cancel()

            self.app.push_screen(MetadataReview(event["metadata"]), finish)
        elif kind in {"inspection", "recovery"}:
            value = (
                event["metadata"]
                if kind == "inspection"
                else {
                    "observed_ids": event["created_ids"],
                    "attempted_ids": event["attempted_ids"],
                    "automatic_rollback": False,
                }
            )
            self._details.append(json.dumps(value, indent=2, ensure_ascii=True))
            self.query_one("#backup-operation-summary", Static).update(
                "\n\n".join(self._details)
            )

    def _finished(self, result):
        if (
            isinstance(self.app.screen, PrivateSecretScreen)
            and self.app.screen.transport is self.transport
        ):
            self.app.screen.dismiss()
        elif isinstance(self.app.screen, MetadataReview):
            # Child may finish/cancel while its nonsecret review was displayed.
            self.app.screen.dismiss(None)
        self.result, self.finished = result, True
        self._restore_interrupt()
        self.app._private_interaction = False
        self._clock.stop()
        self.query_one("#backup-operation-progress", ProgressBar).display = False
        self.query_one("#backup-operation-cancel", Button).disabled = True
        self.query_one("#backup-operation-close", Button).disabled = False
        if result is None:
            status = "Operation outcome unverified. Inspect the explicit target before any retry."
        elif result["code"] == 0:
            status = "Operation completed and acknowledged. Restored workspaces remain stopped."
        else:
            notice = result.get("notice")
            status = (
                (notice["title"] + ": " + notice["message"])
                if notice
                else "Operation did not complete."
            )
        self.query_one("#backup-operation-stage", Static).update(status)
        self.query_one("#backup-operation-close", Button).focus()

    def on_button_pressed(self, event):
        if event.button.id == "backup-operation-cancel":
            event.stop()
            self.action_cancel_operation()
        elif event.button.id == "backup-operation-close" and self.finished:
            event.stop()
            self.dismiss(self.result)

    def action_cancel_operation(self):
        if not self.finished:
            accepted = self.transport.cancel()
            self.query_one("#backup-operation-cancel", Button).disabled = True
            self.query_one("#backup-operation-stage", Static).update(
                "Completion already verified; waiting for the result display…"
                if accepted is False
                else "Cancellation requested; waiting for cleanup…"
            )

    def action_cancel_private_input(self):
        self.action_cancel_operation()

    def _restore_interrupt(self):
        handler = getattr(self, "_interrupt_handler", None)
        if handler is not None:
            if signal.getsignal(signal.SIGINT) is handler:
                signal.signal(signal.SIGINT, self._previous_interrupt)
            self._interrupt_handler = None

    def on_unmount(self):
        self._restore_interrupt()
        if not self.finished:
            self.transport.cancel()
