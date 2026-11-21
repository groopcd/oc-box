"""Opt-in terminal framing for host pathname paste and private OSC 72 packets.

This is not evidence that a paste came from a drag, that the composer is idle,
or that the bytes were not a terminal reply. Only the host-input relay may use
``PasteInput``; child output belongs to the separate ``OutputFilter``. Neither
class reads files, consults UI state, decodes with replacement, or uses timers.
Callbacks are trusted policy code, not a way to establish input provenance.
"""

_ESC = 0x1B
_BEL = 0x07
_CSI = 0x9B
_ST = 0x9C
_OSC = 0x9D
_STRINGS = {0x90: "dcs", 0x98: "sos", _OSC: "osc", 0x9E: "pm", 0x9F: "apc"}
_ESC_STRINGS = {
    ord("P"): "dcs",
    ord("X"): "sos",
    ord("]"): "osc",
    ord("^"): "pm",
    ord("_"): "apc",
}
_PASTE_START = b"\x1b[200~"
_PASTE_END = b"\x1b[201~"

# Explicit Ctrl+Y only. CSI-u press events grant a clipboard read;
# repeat/release events consume the reserved shortcut without granting again.
# Legacy Ctrl+Y is the C0 byte 0x19, accepted only at top-level host input.
# Legacy/ModifyOtherKeys have no separate event type. No printable-text guesses
# or aliases for the earlier Ctrl+Alt+Shift+V shortcut.
_CLIPBOARD_CONTROL = b"\x19"
_CLIPBOARD_KEYS = {
    **{
        key + b";" + modifiers + suffix: event in (b"", b":1")
        for key in (b"121", b"121:89", b"121:89:121")
        for modifiers in (b"5", b"69", b"133", b"197")  # Optional Caps/Num Lock.
        for event, suffix in (
            (b"", b"u"),
            (b":1", b":1u"),
            (b":2", b":2u"),
            (b":3", b":3u"),
        )
    },
    b"27;5;121~": True,
}
# F8 is reserved only by a TUI with a host PNG proposal handler. Ignore
# distinct repeat/release events; never accept a printable/pasted alias.
_HOST_PREVIEW_KEYS = {
    b"19~": True,
    **{
        b"19;" + mods + suffix: suffix in (b"~", b":1~")
        for mods in (b"1", b"65", b"129", b"193")
        for suffix in (b"~", b":1~", b":2~", b":3~")
    },
}

# Includes the ASCII "72;" selector, but not the introducer or terminator.
# The broker must independently validate its protocol/chunk/session limits.
MAX_OSC72 = 8192

# Kitty v0.50.0 vt-parser.c dispatch_osc() reads MIN(limit, 5u) digits.
# This bounded accumulator cannot overflow; do not emulate integer wrapping or
# mistake arbitrarily long leading-zero strings for its numeric selector 72.
_OSC_SELECTOR_DIGITS = 5


class _Units:
    """Yield (code point, original bytes), with None for malformed encoding.

    Standalone 8-bit C1 controls and UTF-8 encoded C1 controls are both framed.
    A valid UTF-8 continuation never independently becomes a C1 control.
    Malformed multibyte sequences retain their continuation bytes as one invalid
    unit; those bytes are never reparsed as C1 terminators. A non-continuation
    which invalidates a prefix is processed separately, so a following actual
    ESC introducer can still be filtered. At most three bytes remain pending.
    """

    def __init__(self):
        self.pending = bytearray()
        self.remaining = 0
        self.low = 0x80
        self.high = 0xBF
        self.code = 0
        self.invalid = False

    def feed(self, content):
        for value in content:
            if self.pending:
                if 0x80 <= value <= 0xBF:
                    self.pending.append(value)
                    if not self.low <= value <= self.high:
                        self.invalid = True
                    self.code = (self.code << 6) | (value & 0x3F)
                    self.remaining -= 1
                    self.low, self.high = 0x80, 0xBF
                    if not self.remaining:
                        raw = bytes(self.pending)
                        self.pending.clear()
                        yield None if self.invalid else self.code, raw
                    continue
                raw = bytes(self.pending)
                self.pending.clear()
                self.remaining = 0
                yield None, raw
            if value <= 0x9F:
                yield value, bytes((value,))
            elif 0xC0 <= value <= 0xDF:
                self.pending.append(value)
                self.remaining, self.code = 1, value & 0x1F
                self.low, self.high = 0x80, 0xBF
                self.invalid = value < 0xC2
            elif 0xE0 <= value <= 0xEF:
                self.pending.append(value)
                self.remaining, self.code = 2, value & 0x0F
                self.low = 0xA0 if value == 0xE0 else 0x80
                self.high = 0x9F if value == 0xED else 0xBF
                self.invalid = False
            elif 0xF0 <= value <= 0xF7:
                self.pending.append(value)
                self.remaining, self.code = 3, value & 0x07
                self.low = 0x90 if value == 0xF0 else 0x80
                self.high = 0x8F if value == 0xF4 else 0xBF
                self.invalid = value > 0xF4
            else:
                yield None, bytes((value,))

    def finish(self):
        raw = bytes(self.pending)
        self.pending.clear()
        self.remaining = 0
        return raw


class _Framing:
    """Shared bounded framing, with disjoint input and output actions."""

    def __init__(
        self,
        *,
        output_filter,
        transform=None,
        on_osc72=None,
        on_clipboard=None,
        on_host_preview=None,
        max_paste=0,
        reserve_osc72=False,
    ):
        # Direction is immutable. Disabling input callbacks must not switch
        # host replies to the child-output terminal's more permissive framing.
        self._child_output = output_filter
        self._reserve_osc72 = reserve_osc72
        self._transform = transform
        self._on_osc72 = on_osc72
        self._on_clipboard = on_clipboard
        self._on_host_preview = on_host_preview
        self._max_paste = max_paste
        self._units = _Units()
        self._state = "text"
        self._held = bytearray()
        self._closed = False
        self._disabled = False
        self._disable_reason = None
        self._string_kind = None
        self._string_escape = False
        self._osc_probe = None
        self._osc_number = 0
        self._osc_target = False
        self._osc_discard = False
        self._osc_intro_length = 0
        self._csi_candidate = None
        self._csi_ascii = False
        self._csi_intermediate = False
        self._csi_number = None
        self._paste_length = 0
        self._paste_end = 0
        self._paste_passthrough = False

    def feed(self, content):
        if self._closed:
            raise ValueError("terminal framing stream is finished")
        if self._disabled and not self._reserve_osc72:
            return content
        output = bytearray()
        for code, raw in self._units.feed(content):
            if self._disabled and not self._reserve_osc72:
                output.extend(raw)
            elif code is None:
                if self._child_output or self._reserve_osc72:
                    if not self._child_output:
                        self._disable_callbacks()
                    if self._state == "paste":
                        self._leave_paste(output)
                    self._invalid_output(raw, output)
                else:
                    self._disable(output, raw)
            else:
                self._consume(code, raw, output)
        if self._disabled and not self._reserve_osc72:
            output.extend(self._units.finish())
        return bytes(output)

    def finish(self):
        if self._closed:
            return b""
        self._closed = True
        pending = self._units.finish()
        # A truncated reserved OSC selector/packet must not disclose its body.
        discard = (
            (self._child_output or self._reserve_osc72)
            and self._state == "string"
            and (self._osc_target or self._reserved_selector())
        )
        result = b"" if discard else bytes(self._held) + pending
        self._held.clear()
        if self._transform is not None:
            if self._state in ("paste", "string"):
                self._disable_callbacks("input-incomplete")
            self._disabled = True
        return result

    def _flush(self, output, raw=b""):
        output.extend(self._held)
        self._held.clear()
        output.extend(raw)

    def _disable(self, output, raw=b""):
        self._flush(output, raw)
        self._disable_callbacks()
        self._state = "text"

    def _disable_callbacks(self, reason="input-framing"):
        self._disabled = True
        if self._disable_reason is None:
            self._disable_reason = reason
        if self._reserve_osc72 and self._state == "string" and self._osc_target:
            self._osc_discard = True
            self._held.clear()

    def _framing_only(self):
        return self._child_output or self._disabled

    def _reserved_selector(self):
        return self._osc_probe is not None and self._osc_number == 72

    def _leave_paste(self, output):
        # Only the already-recognized, at-most-five-byte closing-marker prefix
        # can contain a held introducer. Carry it into framing-only recovery,
        # not into another paste/path callback or a scan of the paste body.
        suffix_length = self._paste_end
        suffix = bytes(self._held[-suffix_length:]) if suffix_length else b""
        if suffix_length:
            output.extend(self._held[:-suffix_length])
            self._held.clear()
        else:
            self._flush(output)
        self._state = "text"
        self._paste_end = 0
        self._paste_passthrough = False
        for value in suffix:
            self._consume(value, bytes((value,)), output)

    def _refuse_unit(self, code, raw, output):
        if not self._reserve_osc72:
            self._disable(output, raw)
            return
        self._disable_callbacks()
        if self._state == "string" and self._osc_target:
            self._string_unit(code, raw, output)
        elif self._state in ("escape", "intermediate"):
            self._escape_unit(code, raw, output)
        elif self._state == "csi":
            self._csi_unit(code, raw, output)
        else:
            if self._state == "paste":
                self._leave_paste(output)
            else:
                self._flush(output)
                self._state = "text"
            self._consume(code, raw, output)

    def _invalid_output(self, raw, output):
        if self._state == "string":
            if self._reserved_selector() and (
                self._child_output or self._reserve_osc72
            ):
                # Missing-separator reservation also applies when that first
                # body unit is malformed UTF-8 rather than an ASCII token.
                self._osc_probe = None
                self._osc_target = True
                if not self._child_output:
                    self._disable_callbacks()
                self._osc_discard = True
                self._held.clear()
            if not self._osc_target:
                self._flush(output, raw)
                self._osc_probe = None
            self._string_escape = False
        else:
            if len(raw) > 1 and raw.endswith(b"\x9d"):
                # A malformed UTF-8 unit cannot establish that its last byte
                # was merely a continuation, rather than a raw OSC introducer.
                # Preserve the bad prefix and probe only that OSC boundary.
                # Never do this inside a string: reinterpreting a bad unit's
                # C1 ST there could expose a reserved packet's remaining body.
                self._flush(output, raw[:-1])
                self._start_string("osc", raw[-1:], output)
            else:
                self._flush(output, raw)
                self._state = "text"

    def _start_csi(self, prefix):
        self._state = "csi"
        self._held[:] = prefix
        self._csi_candidate = b""
        self._csi_ascii = prefix == b"\x1b["
        self._csi_intermediate = False
        self._csi_number = 0

    def _start_string(self, kind, prefix, output):
        self._state = "string"
        self._string_kind = kind
        self._string_escape = False
        self._osc_target = False
        self._osc_discard = False
        self._osc_number = 0
        self._osc_intro_length = len(prefix)
        self._osc_probe = (
            b""
            if kind == "osc"
            and (self._child_output or self._reserve_osc72 or self._on_osc72)
            else None
        )
        if self._osc_probe is None:
            output.extend(prefix)
        else:
            self._held[:] = prefix

    def _consume(self, code, raw, output):
        if self._state == "paste":
            self._paste_unit(code, raw, output)
        elif self._state == "string":
            self._string_unit(code, raw, output)
        elif self._state in ("escape", "intermediate"):
            self._escape_unit(code, raw, output)
        elif self._state == "csi":
            self._csi_unit(code, raw, output)
        elif code == _ESC:
            self._state = "escape"
            self._held[:] = raw
        elif code == _CSI:
            self._start_csi(raw)
        elif code in _STRINGS:
            self._start_string(_STRINGS[code], raw, output)
        elif (
            not self._framing_only()
            and self._on_clipboard is not None
            and raw == _CLIPBOARD_CONTROL
        ):
            self._clipboard_press(raw, output)
        else:
            output.extend(raw)

    def _clipboard_press(self, original, output):
        try:
            replacement = self._on_clipboard()
        except Exception:
            self._disable_callbacks("clipboard-callback")
            output.extend(original)
            return
        if isinstance(replacement, bytes):
            output.extend(_PASTE_START + replacement + _PASTE_END)
        elif replacement is not None:
            self._disable_callbacks("clipboard-callback")
            output.extend(original)

    def _escape_unit(self, code, raw, output):
        if self._state == "escape":
            if code == ord("["):
                prefix = bytes(self._held) + raw
                self._start_csi(prefix)
                return
            if code in _ESC_STRINGS:
                prefix = bytes(self._held) + raw
                self._held.clear()
                self._start_string(_ESC_STRINGS[code], prefix, output)
                return
        if 0x20 <= code <= 0x2F:
            self._flush(output, raw)
            self._state = "intermediate"
        elif 0x30 <= code <= 0x7E:
            self._flush(output, raw)
            self._state = "text"
        elif not self._framing_only():
            self._refuse_unit(code, raw, output)
        elif code < 0x20 and code != _ESC:
            # C0 execution does not end an escape sequence. The already emitted
            # introducer cannot be retracted, but no reserved body is emitted.
            self._flush(output, raw)
        else:
            self._flush(output)
            self._state = "text"
            self._consume(code, raw, output)

    def _csi_unit(self, code, raw, output):
        parameter = 0x30 <= code <= 0x3F and not self._csi_intermediate
        intermediate = 0x20 <= code <= 0x2F
        final = 0x40 <= code <= 0x7E
        if not (parameter or intermediate or final):
            if not self._framing_only():
                self._refuse_unit(code, raw, output)
            elif code < 0x20 and code != _ESC:
                self._flush(output, raw)
                self._csi_candidate = None
            else:
                self._flush(output)
                self._state = "text"
                self._consume(code, raw, output)
            return
        if intermediate:
            self._csi_intermediate = True
        if not final:
            if parameter and ord("0") <= code <= ord("9"):
                if self._csi_number is not None:
                    self._csi_number = min(
                        1000, self._csi_number * 10 + code - ord("0")
                    )
            else:
                self._csi_number = None
        candidate = None
        if self._csi_candidate is not None:
            candidate = self._csi_candidate + raw
            candidates = (b"200~", b"201~")
            if self._on_clipboard is not None and not self._child_output:
                candidates += tuple(_CLIPBOARD_KEYS)
            if self._on_host_preview is not None:
                candidates += tuple(_HOST_PREVIEW_KEYS)
            if not any(value.startswith(candidate) for value in candidates):
                candidate = None
        self._csi_candidate = candidate
        if candidate is None:
            self._flush(output, raw)
        else:
            self._held.extend(raw)
        if not final:
            return
        if (
            not self._framing_only()
            and self._csi_ascii
            and self._on_host_preview is not None
            and candidate in _HOST_PREVIEW_KEYS
        ):
            original = bytes(self._held)
            self._held.clear()
            self._state = "text"
            if not self._on_host_preview(_HOST_PREVIEW_KEYS[candidate]):
                output.extend(original)
            return
        if (
            not self._framing_only()
            and self._csi_ascii
            and self._on_clipboard is not None
            and candidate in _CLIPBOARD_KEYS
        ):
            original = bytes(self._held)
            self._held.clear()
            self._state = "text"
            if not _CLIPBOARD_KEYS[candidate]:
                return
            self._clipboard_press(original, output)
            return
        if (
            not self._framing_only()
            and code == ord("~")
            and self._csi_number in (200, 201)
        ):
            if candidate != b"200~" or not self._csi_ascii:
                # This includes orphan ends, raw/encoded C1 paste markers and
                # numerically equivalent markers with leading zeroes. Never
                # re-arm inside a possible paste interpreted by another client.
                self._disable(output)
            else:
                self._state = "paste"
                self._paste_length = 0
                self._paste_end = 0
                self._paste_passthrough = False
            return
        self._flush(output)
        self._state = "text"

    def _paste_unit(self, code, raw, output):
        if self._paste_end or code == _ESC:
            if raw != _PASTE_END[self._paste_end : self._paste_end + 1]:
                self._refuse_unit(code, raw, output)
                return
            self._held.extend(raw)
            self._paste_end += 1
            if self._paste_end == len(_PASTE_END):
                self._state = "text"
                if self._paste_passthrough:
                    self._flush(output)
                    self._paste_passthrough = False
                    return
                original = bytes(self._held)
                payload = original[len(_PASTE_START) : -len(_PASTE_END)]
                self._held.clear()
                try:
                    replacement = self._transform(payload)
                except Exception:
                    self._disable_callbacks("paste-callback")
                    output.extend(original)
                    return
                if replacement is None:
                    output.extend(original)
                elif isinstance(replacement, bytes):
                    output.extend(_PASTE_START)
                    output.extend(replacement)
                    output.extend(_PASTE_END)
                else:
                    self._disable_callbacks("paste-callback")
                    output.extend(original)
            return
        if (code < 0x20 and code not in (0x09, 0x0A, 0x0D)) or 0x7F <= code <= 0x9F:
            self._refuse_unit(code, raw, output)
            return
        if self._paste_passthrough:
            output.extend(raw)
        elif self._paste_length + len(raw) > self._max_paste:
            # The path-capture limit is not a framing error. Stream the
            # remaining ordinary paste without a callback, but stay opaque
            # until its canonical end. Only a partial terminator is held.
            self._flush(output, raw)
            self._paste_passthrough = True
        else:
            self._held.extend(raw)
            self._paste_length += len(raw)

    def _selector_unit(self, code, raw, output):
        extra = raw
        if ord("0") <= code <= ord("9"):
            self._held.extend(raw)
            self._osc_probe += raw
            self._osc_number = self._osc_number * 10 + code - ord("0")
            if len(self._osc_probe) < _OSC_SELECTOR_DIGITS:
                return
            # The fifth digit ends Kitty's selector even if more digits follow.
            extra = b""
        canonical = self._osc_probe == b"72" and extra == b";"
        reserved = self._reserved_selector() and (
            self._child_output or self._reserve_osc72
        )
        self._osc_probe = None
        if not (canonical or reserved):
            self._flush(output, extra)
            return
        self._osc_target = True
        if reserved and not canonical and not self._child_output:
            # Only canonical ASCII 72; is a callback/authorization packet.
            self._disable_callbacks()
        self._osc_discard = self._child_output or (
            self._reserve_osc72 and self._disabled
        )
        if self._osc_discard:
            self._held.clear()
        else:
            self._held.extend(extra)

    def _string_unit(self, code, raw, output):
        escaped_st = self._string_escape and code == ord("\\")
        if (
            self._osc_target
            and not self._osc_discard
            and self._string_escape
            and not escaped_st
        ):
            self._refuse_unit(code, raw, output)
            return
        terminator = (
            code == _ST
            or (code == _BEL and (self._string_kind == "osc" or self._child_output))
            or escaped_st
        )
        if terminator:
            reserved_selector = self._reserved_selector() and (
                self._child_output or self._reserve_osc72
            )
            if self._reserve_osc72 and reserved_selector:
                self._disable_callbacks()
            if self._osc_target and not self._osc_discard:
                original = bytes(self._held) + raw
                content = bytes(self._held)[
                    self._osc_intro_length : -1 if escaped_st else None
                ]
                self._held.clear()
                self._state = "text"
                try:
                    consumed = (
                        self._on_osc72(content) if self._on_osc72 is not None else False
                    )
                except Exception:
                    consumed = None
                if consumed is True:
                    pass
                elif consumed is False:
                    if not self._reserve_osc72:
                        output.extend(original)
                elif isinstance(consumed, bytes):
                    output.extend(consumed)
                else:
                    if not self._reserve_osc72:
                        output.extend(original)
                    self._disable_callbacks("osc72-callback")
                return
            if self._osc_discard or reserved_selector:
                self._held.clear()
            else:
                self._flush(output, raw)
            self._state = "text"
            return
        if self._osc_target:
            if not self._osc_discard:
                if (
                    self._string_escape
                    or (code < 0x20 and code != _ESC)
                    or 0x7F <= code <= 0x9F
                    or (
                        len(self._held) - self._osc_intro_length + len(raw)
                        > MAX_OSC72 + (1 if code == _ESC else 0)
                    )
                ):
                    self._refuse_unit(code, raw, output)
                    return
                self._held.extend(raw)
        elif self._osc_probe is not None:
            self._selector_unit(code, raw, output)
        else:
            output.extend(raw)
        # Interior ESC/CSI do not cancel opaque strings. BEL is deliberately
        # direction-specific: Kitty child output ends every string on BEL,
        # while host input keeps non-OSC replies opaque through ST. Consecutive
        # ESC bytes allow only the last ESC followed by backslash to end them.
        self._string_escape = code == _ESC


class PasteInput(_Framing):
    """Intercept complete ASCII, top-level bracketed pastes on host stdin only.

    ``transform(payload)`` returns replacement bytes or None to retain the
    original frame. ``on_osc72(content)`` receives the complete content beginning
    with ASCII ``72;``; exactly True consumes it, False retains the packet, and
    bytes replace it in place (including b"" to consume). Replacement precedes
    following keys in the same feed() call; no wrapper append/reordering occurs.
    Policy callbacks must validate their own paths, metadata and replacement.
    Exceptions/invalid return types disable interception.

    Ambiguous/malformed encoding or paste/control framing permanently sets
    ``disabled``. Normally the original and subsequent bytes pass unchanged;
    there is no interior rescan or timing-based re-arm. Over-limit ordinary
    pastes stream unchanged until their exact canonical end without callbacks.
    With ``reserve_osc72=True``, refused/malformed/oversized private packets and
    partial private packets at EOF are discarded, never forwarded to the child.
    After failure only framing/reservation continues, with all authorization
    callbacks disabled. In reserve mode False also consumes; bytes still replace
    in place. Default False retains the generic callback's forwarding contract.
    Numeric selector aliases recognized by Kitty's five-digit dispatcher are
    discarded in reserve mode, never passed to the canonical-packet callback.
    Input's non-OSC replies stay opaque through ST even after failure.
    Ordinary opaque strings stream without accumulating their bodies. At most
    ``max(max_paste + 12, MAX_OSC72 + 3)`` original bytes are held, plus at most
    three pending UTF-8 bytes and constant framing state, in persistent parser
    state. Input/output and temporary callback/frame copies are also bounded by
    the supplied chunk and frame limits (callback-return size is trusted).
    finish() flushes incomplete ordinary input without granting, discards partial
    reserved packets, and seals the stream; repeated finish() is harmless.
    """

    def __init__(
        self,
        transform,
        *,
        on_osc72=None,
        on_clipboard=None,
        on_host_preview=None,
        max_paste=16384,
        reserve_osc72=False,
    ):
        if not callable(transform):
            raise TypeError("transform must be callable")
        if on_osc72 is not None and not callable(on_osc72):
            raise TypeError("on_osc72 must be callable or None")
        if on_clipboard is not None and not callable(on_clipboard):
            raise TypeError("on_clipboard must be callable or None")
        if on_host_preview is not None and not callable(on_host_preview):
            raise TypeError("on_host_preview must be callable or None")
        if (
            isinstance(max_paste, bool)
            or not isinstance(max_paste, int)
            or max_paste < 0
        ):
            raise ValueError("max_paste must be a nonnegative integer")
        if not isinstance(reserve_osc72, bool):
            raise TypeError("reserve_osc72 must be a boolean")
        super().__init__(
            output_filter=False,
            transform=transform,
            on_osc72=on_osc72,
            on_clipboard=on_clipboard,
            on_host_preview=on_host_preview,
            max_paste=max_paste,
            reserve_osc72=reserve_osc72,
        )

    @property
    def disabled(self):
        """Whether interception is permanently refused for this input stream."""
        return self._disabled

    @property
    def disable_reason(self):
        """First fixed reason for failing closed, not normal stream closure."""
        return self._disable_reason

    def set_host_preview(self, callback):
        """Install the host-local F8 handler before any input is parsed."""
        self._on_host_preview = callback

    def safe_for_host_prompt(self):
        """Do not steal the tail of a paste/reply following F8 in one read."""
        return (
            not self._closed
            and not self._disabled
            and self._state == "text"
            and not self._held
            and not self._units.pending
        )


MAX_OUTPUT_CONTROL = 262144 - 256
MAX_OUTPUT_PRIVATE = 65536
_OUTPUT_NEUTRAL = b" \x1b\\"
_CSI_EMBEDDED = frozenset((7, 8, 9, 10, 11, 12, 13, 14, 15))


class ReservationError(RuntimeError):
    """The opted-in Kitty relay must stop, never continue unfiltered."""


class _KittyBoundary:
    """Bounded C0 framing of emitted bytes, not terminal actions or C1 grammar.

    Kitty v0.50.0 consumes an unknown byte after ESC and returns to normal,
    including NUL and a second ESC. Its charset/designation forms consume one
    more byte. CSI ordering follows the three source framing states, with a
    conservative separator guard below its 256-parameter recovery boundary.
    """

    def __init__(self):
        self.state = "text"
        self.units = _Units()
        self.length = 0
        self.string_escape = False
        self.csi_digits = False
        self.csi_separators = 0

    def consume(self, value):
        if self.state == "text":
            # Only literal C0 ESC changes Kitty's normal-mode grammar. Raw and
            # UTF-8 C1 values are text here; pending UTF-8 still fences injection.
            for code, _ in self.units.feed(bytes((value,))):
                if code == _ESC:
                    self.state = "escape"
                    self.length = 1
            return
        self.length += 1
        if self.length > MAX_OUTPUT_CONTROL:
            raise ReservationError("host-drop output control limit exceeded")
        if self.state == "escape":
            if value == ord("["):
                self.state = "csi_start"
                self.csi_digits = False
                self.csi_separators = 0
            elif value in b"]PX^_":
                self.state = "string"
                self.string_escape = False
            elif value in b"%()*+-./ #":
                self.state = "extra"
            else:
                # Do not reinterpret ESC ESC as a new escape introducer.
                self.state = "text"
        elif self.state == "extra":
            self.state = "text"
        elif self.state == "string":
            if value == _BEL or (self.string_escape and value == ord("\\")):
                self.state = "text"
            else:
                self.string_escape = value == _ESC
        elif value not in _CSI_EMBEDDED:
            if value in b";:":
                self.csi_separators += 1
                if self.csi_separators > 64:
                    raise ReservationError("host-drop output CSI limit exceeded")
            digit = ord("0") <= value <= ord("9")
            secondary = 0x20 <= value <= 0x2F
            if self.state == "csi_start":
                if digit or value in b";?><=" or value == ord("-"):
                    self.state = "csi_body"
                    self.csi_digits = digit or value == ord("-")
                elif secondary:
                    self.state = "csi_post"
                else:
                    self.state = "text"
            elif self.state == "csi_body":
                if digit:
                    self.csi_digits = True
                elif value in b";:":
                    self.csi_digits = False
                elif secondary:
                    if value == ord("-") and not self.csi_digits:
                        self.csi_digits = True
                    else:
                        self.state = "csi_post"
                else:
                    self.state = "text"
            else:  # csi_post: final or invalid byte is consumed, not restarted.
                self.state = "text"
        if self.state == "text":
            self.length = 0


class OutputFilter:
    """Kitty-only conservative C0 OSC72 wire reservation, never a C1 union.

    Every literal ESC ] numeric-72 header is reserved, even inside another
    string. Kitty's five-digit selector/optional separator rules apply. Valid
    graphics base64 and keyboard CSI contain no such literal private pattern;
    all other bytes pass unchanged. C1-only output is ordinary target text.

    A blocked span becomes SPACE + C0 ST, not empty output: the space prevents
    numeric-header joins and ST closes known C0 string contexts. Injection
    readiness monitors the actual emitted stream, including that replacement.
    No input parser, callback, generic C1 filter, or renderer is involved.

    Headers hold at most seven bytes. Private bodies are streamed to discard.
    Raw emitted control spans are capped at MAX_OUTPUT_CONTROL, with a 256-byte
    margin below Kitty's 262144-byte recovery threshold. Discarded private
    packets have the separate, lower MAX_OUTPUT_PRIVATE cap; CSI separators
    are capped at 64, below its parameter recovery boundary. Exceeding a cap
    raises sticky ReservationError before feed() can return that unsafe chunk.
    This class is not used by the byte-transparent Konsole profile.
    """

    def __init__(self):
        self._boundary = _KittyBoundary()
        self._units = self._boundary.units
        self._held = bytearray()
        self._state = "text"
        self._number = 0
        self._digits = 0
        self._private_length = 0
        self._private_escape = False
        self._closed = False
        self._failed = False

    def _emit(self, content, output):
        for value in content:
            self._boundary.consume(value)
            output.append(value)

    def _discard(self, value, output):
        self._private_length += 1
        if self._private_length > MAX_OUTPUT_PRIVATE:
            raise ReservationError("host-drop private output limit exceeded")
        if value == _BEL or (self._private_escape and value == ord("\\")):
            self._emit(_OUTPUT_NEUTRAL, output)
            self._state = "text"
        else:
            self._private_escape = value == _ESC

    def _consume(self, value, output):
        if self._state == "private":
            self._discard(value, output)
        elif self._state == "header":
            if ord("0") <= value <= ord("9"):
                self._held.append(value)
                self._digits += 1
                self._number = self._number * 10 + value - ord("0")
                if self._digits < _OSC_SELECTOR_DIGITS:
                    return
                extra = None
            else:
                extra = value
            if self._number == 72:
                self._state = "private"
                self._private_length = len(self._held)
                self._private_escape = False
                self._held.clear()
                if extra is not None:
                    self._discard(extra, output)
            else:
                self._emit(self._held, output)
                self._held.clear()
                self._state = "text"
                if extra is not None:
                    self._consume(extra, output)
        elif self._state == "escape":
            if value == ord("]"):
                self._held.append(value)
                self._state = "header"
                self._number = self._digits = 0
            else:
                self._emit(self._held, output)
                self._held.clear()
                self._state = "text"
                self._consume(value, output)
        elif value == _ESC:
            self._held.append(value)
            self._state = "escape"
        else:
            self._emit(bytes((value,)), output)

    def _check_open(self):
        if self._failed:
            raise ReservationError("host-drop output reservation stopped")
        if self._closed:
            raise ValueError("terminal framing stream is finished")

    def feed(self, content):
        self._check_open()
        output = bytearray()
        try:
            for value in content:
                self._consume(value, output)
        except ReservationError:
            self._failed = True
            raise
        return bytes(output)

    def finish(self):
        if self._failed:
            raise ReservationError("host-drop output reservation stopped")
        if self._closed:
            return b""
        output = bytearray()
        try:
            if self._state == "private" or (
                self._state == "header" and self._number == 72
            ):
                self._emit(_OUTPUT_NEUTRAL, output)
            else:
                self._emit(self._held, output)
        except ReservationError:
            self._failed = True
            raise
        self._held.clear()
        self._closed = True
        return bytes(output)

    def safe_to_inject(self):
        """A boundary in emitted Kitty C0 output, not composer/UI readiness."""
        return (
            not self._closed
            and not self._failed
            and self._state == "text"
            and not self._held
            and self._boundary.state == "text"
            and not self._units.pending
        )
