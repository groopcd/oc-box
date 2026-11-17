"""Offline framing regressions, not terminal provenance or host acceptance."""

import unittest

from oc_box.host_drop_input import (
    MAX_OSC72,
    MAX_OUTPUT_CONTROL,
    MAX_OUTPUT_PRIVATE,
    OutputFilter,
    PasteInput,
    ReservationError,
)


START = b"\x1b[200~"
END = b"\x1b[201~"
REPLACEMENT = b"/container/private/image.png"
NEUTRAL = b" \x1b\\"
OSC_INTROS = (b"\x1b]", b"\x9d", b"\xc2\x9d")
STRING_ENDS = (b"\x1b\\", b"\x9c", b"\xc2\x9c")
OSC_ENDS = (*STRING_ENDS, b"\x07")
STRING_INTROS = {
    "osc": OSC_INTROS,
    "dcs": (b"\x1bP", b"\x90", b"\xc2\x90"),
    "sos": (b"\x1bX", b"\x98", b"\xc2\x98"),
    "pm": (b"\x1b^", b"\x9e", b"\xc2\x9e"),
    "apc": (b"\x1b_", b"\x9f", b"\xc2\x9f"),
}


def paste(payload):
    return START + payload + END


def osc72(payload=b"i=synthetic;encoded", *, intro=b"\x1b]", end=b"\x1b\\"):
    return intro + b"72;" + payload + end


def fragmentations(content):
    """Whole, single bytes, empty chunks, and every possible two-part split."""
    yield "whole", (content,)
    yield "single_bytes", tuple(bytes((value,)) for value in content)
    yield "empty_chunks", (b"", content, b"", b"")
    for offset in range(1, len(content)):
        yield f"split_{offset}", (content[:offset], content[offset:])


def private_c0_offsets(content):
    """Audit the final wire independently of filter state, including joins."""
    found = []
    for offset in range(len(content) - 1):
        if content[offset : offset + 2] != b"\x1b]":
            continue
        number = 0
        for value in content[offset + 2 : offset + 7]:
            if not ord("0") <= value <= ord("9"):
                break
            number = number * 10 + value - ord("0")
        if number == 72:
            found.append(offset)
    return found


class FramingCase(unittest.TestCase):
    def assert_input(
        self,
        content,
        expected,
        *,
        payloads=(),
        packets=(),
        disabled=False,
        replacement=REPLACEMENT,
        packet_result=False,
        max_paste=16384,
        packet_handler=True,
        reserve_osc72=False,
    ):
        for fragmentation, chunks in fragmentations(content):
            with self.subTest(fragmentation=fragmentation):
                seen_pastes = []
                seen_packets = []

                def transform(payload):
                    seen_pastes.append(payload)
                    return replacement

                def on_packet(payload):
                    seen_packets.append(payload)
                    return packet_result

                parser = PasteInput(
                    transform,
                    on_osc72=on_packet if packet_handler else None,
                    max_paste=max_paste,
                    reserve_osc72=reserve_osc72,
                )
                result = b"".join(parser.feed(chunk) for chunk in chunks)
                self.assertEqual(parser.disabled, disabled)
                result += parser.finish()
                self.assertEqual(result, expected)
                self.assertEqual(seen_pastes, list(payloads))
                self.assertEqual(seen_packets, list(packets))
                self.assertEqual(parser.finish(), b"")

    def assert_output(self, content, expected):
        for fragmentation, chunks in fragmentations(content):
            with self.subTest(fragmentation=fragmentation):
                parser = OutputFilter()
                result = b"".join(parser.feed(chunk) for chunk in chunks)
                result += parser.finish()
                self.assertEqual(result, expected)
                self.assertEqual(parser.finish(), b"")


class PasteInputTests(FramingCase):
    def test_complete_top_level_ascii_pastes_preserve_frames_and_surrounding_order(
        self,
    ):
        content = (
            b"before" + paste(b"/host/a.png") + b"\r" + paste(b"/host/b.png") + b"after"
        )
        expected = (
            b"before" + paste(REPLACEMENT) + b"\r" + paste(REPLACEMENT) + b"after"
        )
        self.assert_input(content, expected, payloads=(b"/host/a.png", b"/host/b.png"))

    def test_refused_paste_and_unframed_paths_are_byte_exact(self):
        content = b"/host/ordinary.png\t" + paste(b"'not an approved path'") + b"\r"
        self.assert_input(
            content,
            content,
            payloads=(b"'not an approved path'",),
            replacement=None,
        )

    def test_unicode_and_whitespace_payloads_are_policy_inputs_not_reparsed_controls(
        self,
    ):
        payload = "'/host/ÜÛÝÞß image.png'\t\r\n".encode()
        self.assert_input(
            paste(payload),
            paste(REPLACEMENT),
            payloads=(payload,),
        )

    def test_empty_paste_and_zero_limit(self):
        self.assert_input(paste(b""), paste(REPLACEMENT), payloads=(b"",), max_paste=0)

    def test_callback_runs_only_after_the_complete_closing_marker(self):
        seen = []
        parser = PasteInput(lambda payload: seen.append(payload) or REPLACEMENT)
        self.assertEqual(parser.feed(b"before" + START + b"path" + END[:-1]), b"before")
        self.assertEqual(seen, [])
        self.assertEqual(parser.feed(END[-1:] + b"\r"), paste(REPLACEMENT) + b"\r")
        self.assertEqual(seen, [b"path"])
        self.assertFalse(parser.disabled)
        self.assertEqual(parser.finish(), b"")

    def test_ordinary_keyboard_and_mode_sequences_are_byte_exact(self):
        content = (
            b"\x03\x04\x09\x0a\x0d\x7f"
            b"\x1b[97;5u\x1b[57358u\x1b[1;5A\x1bOQ"
            b"\x1b[?2004h\x1b[?2004l\x1b(B\x1b7"
            b"\x9b1;5A\xc2\x9b97;5u"
        )
        self.assert_input(content, content)

    def test_title_reply_fake_paste_is_opaque(self):
        response = (
            b"\x1b]lquery answer " + paste(b"/host/not-authorized.png") + b"\x1b\\"
        )
        content = response + paste(b"/host/explicit.png")
        self.assert_input(
            content, response + paste(REPLACEMENT), payloads=(b"/host/explicit.png",)
        )

    def test_all_opaque_control_string_introducers_and_terminators(self):
        for kind, intros in STRING_INTROS.items():
            ends = OSC_ENDS if kind == "osc" else STRING_ENDS
            for intro in intros:
                for end in ends:
                    with self.subTest(kind=kind, intro=intro, end=end):
                        opaque = intro + b"0;answer" + paste(b"/host/no.png") + end
                        content = opaque + paste(b"/host/yes.png")
                        self.assert_input(
                            content,
                            opaque + paste(REPLACEMENT),
                            payloads=(b"/host/yes.png",),
                        )

    def test_only_osc_may_end_with_bel(self):
        for kind, intros in STRING_INTROS.items():
            if kind == "osc":
                continue
            for intro in intros:
                with self.subTest(kind=kind, intro=intro):
                    opaque = (
                        intro
                        + b"reply\x07"
                        + paste(b"/host/bel-is-not-st.png")
                        + b"\x1b\\"
                    )
                    self.assert_input(
                        opaque + paste(b"/host/yes.png"),
                        opaque + paste(REPLACEMENT),
                        payloads=(b"/host/yes.png",),
                    )

    def test_interior_escape_and_csi_never_cancel_an_opaque_string(self):
        interiors = (
            b"\x1b[1;2R",
            b"\x1b]another title",
            b"\x1bP",
            b"\x1bX",
            b"\x1b^",
            b"\x1b_",
            b"\x1b?",
            b"\x1b\x1b",
            b"\x9b1;2R",
            b"\xc2\x9b1;2R",
        )
        for interior in interiors:
            for kind in ("osc", "dcs", "sos", "pm", "apc"):
                with self.subTest(kind=kind, interior=interior):
                    opaque = (
                        STRING_INTROS[kind][0]
                        + b"0;reply"
                        + interior
                        + paste(b"/host/no.png")
                        + b"\x1b\\"
                    )
                    self.assert_input(
                        opaque + paste(b"/host/yes.png"),
                        opaque + paste(REPLACEMENT),
                        payloads=(b"/host/yes.png",),
                    )

    def test_consecutive_escapes_use_only_the_last_escape_for_st(self):
        opaque = b"\x1bPdata\x1b\x1b\\"
        self.assert_input(
            opaque + paste(b"path"),
            opaque + paste(REPLACEMENT),
            payloads=(b"path",),
        )

    def test_utf8_continuations_are_not_raw_c1_string_or_csi_boundaries(self):
        letters = "ÜÛÝÞß".encode()
        for intro in STRING_INTROS["dcs"]:
            with self.subTest(intro=intro):
                opaque = intro + letters + paste(b"/host/no.png") + b"\x1b\\"
                self.assert_input(
                    letters + opaque + paste(letters),
                    letters + opaque + paste(REPLACEMENT),
                    payloads=(letters,),
                )

    def test_utf8_multibyte_sequences_with_c1_valued_continuations(self):
        payload = b"\xe2\x82\x9c\xf0\x9f\x98\x9b"
        self.assert_input(
            payload + paste(payload),
            payload + paste(REPLACEMENT),
            payloads=(payload,),
        )

    def test_malformed_encoding_permanently_disables_without_rescanning(self):
        malformed = {
            "orphan_continuation": b"\xa0",
            "orphan_high_continuation": b"\xbf",
            "overlong_two_byte": b"\xc0\xaf",
            "invalid_lead_c1": b"\xc1",
            "overlong_three_byte": b"\xe0\x80\x80",
            "surrogate": b"\xed\xa0\x80",
            "above_unicode": b"\xf4\x90\x80\x80",
            "invalid_four_byte_lead": b"\xf5",
            "invalid_ff": b"\xff",
            "truncated_before_escape": b"\xc3",
        }
        for name, prefix in malformed.items():
            for opaque in (b"", b"\x1b]0;title", b"\x1bPdata"):
                with self.subTest(case=name, opaque=opaque):
                    content = (
                        opaque
                        + prefix
                        + paste(b"/host/no.png")
                        + b"\x1b\\"
                        + paste(b"later")
                    )
                    self.assert_input(content, content, disabled=True)

    def test_malformed_or_ambiguous_paste_control_matrix_is_never_rearmed(self):
        ambiguous = {
            "orphan_end": END,
            "nested_start": START + b"outer" + START + b"nested" + END + END,
            "escape_in_payload": START + b"path\x1b[A" + END,
            "nul_in_payload": paste(b"path\x00.png"),
            "del_in_payload": paste(b"path\x7f.png"),
            "raw_c1_in_payload": paste(b"path\x85.png"),
            "encoded_c1_in_payload": paste(b"path\xc2\x85.png"),
            "raw_c1_start": b"\x9b200~path" + END,
            "encoded_c1_start": b"\xc2\x9b200~path" + END,
            "raw_c1_end": START + b"path\x9b201~",
            "encoded_c1_end": START + b"path\xc2\x9b201~",
            "zero_prefixed_start": b"\x1b[0200~path" + END,
            "zero_prefixed_end": b"\x1b[0201~",
            "escape_escape": b"\x1b" + START + b"path" + END,
            "control_in_csi": b"\x1b[2\x0300~path" + END,
            "unicode_in_csi": b"\x1b[2\xc3\x9b00~path" + END,
            "malformed_intermediate": b"\x1b[2 0~",
        }
        for name, prefix in ambiguous.items():
            with self.subTest(case=name):
                content = prefix + paste(b"/host/never-rearm.png")
                self.assert_input(content, content, disabled=True)

    def test_limit_counts_payload_bytes_not_code_points_or_marker_bytes(self):
        payload = "ÜÛ".encode()
        self.assert_input(
            paste(payload),
            paste(REPLACEMENT),
            payloads=(payload,),
            max_paste=len(payload),
        )
        content = paste(payload) + paste(b"x")
        self.assert_input(
            content,
            content,
            disabled=True,
            max_paste=len(payload) - 1,
        )

    def test_over_limit_frame_never_rescans_its_interior_or_later_frames(self):
        content = START + b"x" * 17 + paste(b"nested") + END + paste(b"later")
        self.assert_input(content, content, disabled=True, max_paste=16)

    def test_all_incomplete_frame_prefixes_flush_without_grant(self):
        complete = paste("/host/Ü.png".encode())
        for offset in range(1, len(complete)):
            with self.subTest(offset=offset):
                content = complete[:offset]
                self.assert_input(content, content)

    def test_incomplete_utf8_flush_is_byte_exact_without_replacement(self):
        for tail in (b"\xc3", b"\xe2", b"\xe2\x82", b"\xf0\x9f\x98"):
            for prefix in (b"text", START + b"path", b"\x1b]0;title"):
                with self.subTest(tail=tail, prefix=prefix):
                    content = prefix + tail
                    self.assert_input(content, content)

    def test_transform_exception_or_invalid_return_preserves_input_and_disables(self):
        content = paste(b"first") + paste(b"later")
        for result in (False, True, 0, "not bytes", bytearray(b"not bytes")):
            with self.subTest(result=result):
                self.assert_input(
                    content,
                    content,
                    payloads=(b"first",),
                    replacement=result,
                    disabled=True,
                )
        calls = []

        def broken(payload):
            calls.append(payload)
            raise RuntimeError("synthetic policy failure")

        parser = PasteInput(broken)
        self.assertEqual(parser.feed(content) + parser.finish(), content)
        self.assertEqual(calls, [b"first"])
        self.assertTrue(parser.disabled)

    def test_empty_bytes_transform_is_a_valid_empty_paste_not_a_refusal(self):
        self.assert_input(
            paste(b"path"), paste(b""), payloads=(b"path",), replacement=b""
        )

    def test_constructor_rejects_invalid_policy_interfaces_and_limits(self):
        for value in (None, True, 1, b"not callable"):
            with self.subTest(transform=value):
                with self.assertRaises(TypeError):
                    PasteInput(value)
        with self.assertRaises(TypeError):
            PasteInput(lambda value: value, on_osc72=True)
        for value in (-1, True, 1.0, None, "16384"):
            with self.subTest(max_paste=value):
                with self.assertRaises(ValueError):
                    PasteInput(lambda value: value, max_paste=value)

    def test_finish_seals_stream_and_never_grants_a_later_completion(self):
        calls = []
        parser = PasteInput(lambda payload: calls.append(payload))
        self.assertEqual(parser.feed(START + b"unfinished"), b"")
        self.assertEqual(parser.finish(), START + b"unfinished")
        self.assertTrue(parser.disabled)
        self.assertEqual(parser.finish(), b"")
        with self.assertRaises(ValueError):
            parser.feed(END)
        self.assertEqual(calls, [])

    def test_non_target_opaque_strings_do_not_accumulate_their_bodies(self):
        for prefix in (b"\x1b]0;", b"\x1bP", b"\x1bX", b"\x1b^", b"\x1b_G"):
            with self.subTest(prefix=prefix):
                calls = []
                parser = PasteInput(
                    lambda payload: calls.append(payload) or REPLACEMENT
                )
                self.assertEqual(parser.feed(prefix), prefix)
                for _ in range(32):
                    body = b"x" * 8192
                    self.assertEqual(parser.feed(body), body)
                    self.assertLessEqual(len(parser._held), 2)
                    self.assertLessEqual(len(parser._units.pending), 3)
                self.assertEqual(
                    parser.feed(b"\x1b\\" + paste(b"real")),
                    b"\x1b\\" + paste(REPLACEMENT),
                )
                self.assertEqual(calls, [b"real"])
                self.assertFalse(parser.disabled)
                self.assertEqual(parser.finish(), b"")


class OSC72InputTests(FramingCase):
    def test_all_complete_osc_introducers_and_terminators_call_with_content_only(self):
        for intro in OSC_INTROS:
            for end in OSC_ENDS:
                with self.subTest(intro=intro, end=end):
                    content = b"before" + osc72(intro=intro, end=end) + b"\r"
                    self.assert_input(
                        content,
                        b"before\r",
                        packets=(b"72;i=synthetic;encoded",),
                        packet_result=True,
                    )

    def test_false_forwards_original_packet_and_does_not_authorize_its_payload(self):
        content = osc72() + b"\r" + paste(b"/host/explicit.png")
        self.assert_input(
            content,
            osc72() + b"\r" + paste(REPLACEMENT),
            packets=(b"72;i=synthetic;encoded",),
            payloads=(b"/host/explicit.png",),
            packet_result=False,
        )

    def test_bytes_replacement_precedes_following_enter_and_keys_in_same_chunk(self):
        replacement = paste(REPLACEMENT)
        content = b"before" + osc72(b"i=synthetic;eof=1") + b"\r\x1b[Aafter"
        self.assert_input(
            content,
            b"before" + replacement + b"\r\x1b[Aafter",
            packets=(b"72;i=synthetic;eof=1",),
            packet_result=replacement,
        )

    def test_empty_bytes_consumes_instead_of_false_forwarding(self):
        self.assert_input(
            osc72() + b"\r",
            b"\r",
            packets=(b"72;i=synthetic;encoded",),
            packet_result=b"",
        )

    def test_selector_is_ascii_72_semicolon_only_and_metadata_is_not_parser_policy(
        self,
    ):
        alternatives = (b"7;", b"720;", b"73;", b"072;", b"72x;", "７２;".encode())
        for selector in alternatives:
            with self.subTest(selector=selector):
                content = b"\x1b]" + selector + b"opaque" + b"\x07"
                self.assert_input(content, content)
        self.assert_input(
            osc72(b"not-validated-by-framing"),
            b"",
            packets=(b"72;not-validated-by-framing",),
            packet_result=True,
        )

    def test_without_osc_handler_packet_is_streamed_opaque_and_preserved(self):
        # A paste-looking string interior is never a separate authorization.
        content = osc72(paste(b"/host/no.png")) + paste(b"/host/yes.png")
        self.assert_input(
            content,
            osc72(paste(b"/host/no.png")) + paste(REPLACEMENT),
            payloads=(b"/host/yes.png",),
            packet_handler=False,
        )

    def test_packet_inside_other_control_strings_never_reaches_handler(self):
        for kind in ("dcs", "sos", "pm", "apc"):
            for intro in STRING_INTROS[kind]:
                with self.subTest(kind=kind, intro=intro):
                    content = intro + osc72(end=b"\x07") + b"\x1b\\"
                    self.assert_input(content, content)

    def test_ordinary_unicode_in_packet_is_not_a_c1_terminator(self):
        payload = "i=synthetic;ÜÛÝÞß".encode()
        self.assert_input(
            osc72(payload),
            b"",
            packets=(b"72;" + payload,),
            packet_result=True,
        )

    def test_packet_control_ambiguity_never_grants_or_rearms(self):
        malformed = {
            "interior_escape_csi": b"data\x1b[31m",
            "interior_escape_escape": b"data\x1b\x1b",
            "nul": b"data\x00",
            "newline": b"data\n",
            "raw_c1": b"data\x85",
            "encoded_c1": b"data\xc2\x85",
            "invalid_encoding": b"data\xff",
        }
        for name, payload in malformed.items():
            with self.subTest(case=name):
                content = osc72(payload) + paste(b"later")
                self.assert_input(content, content, disabled=True)
        for ending in (b"\x1b\x07", b"\x1b\x9c", b"\x1b\xc2\x9c"):
            with self.subTest(ending=ending):
                content = b"\x1b]72;data" + ending + paste(b"later")
                self.assert_input(content, content, disabled=True)

    def test_exact_packet_limit_excludes_introducer_and_full_terminator(self):
        payload = b"x" * (MAX_OSC72 - 3)
        for intro in OSC_INTROS:
            for ending in OSC_ENDS:
                with self.subTest(intro=intro, ending=ending):
                    seen = []
                    parser = PasteInput(
                        lambda value: value,
                        on_osc72=lambda value: seen.append(value) or True,
                    )
                    content = osc72(payload, intro=intro, end=ending)
                    result = b"".join(parser.feed(bytes((value,))) for value in content)
                    self.assertEqual(result, b"")
                    self.assertFalse(parser.disabled)
                    self.assertEqual(seen, [b"72;" + payload])
                    self.assertEqual(parser.finish(), b"")

    def test_oversized_packet_disables_without_unbounded_capture_or_interior_rescan(
        self,
    ):
        calls = []
        parser = PasteInput(
            lambda value: calls.append(("paste", value)) or REPLACEMENT,
            on_osc72=lambda value: calls.append(("packet", value)) or True,
        )
        prefix = b"\x1b]72;"
        self.assertEqual(parser.feed(prefix), b"")
        body = b"x" * (MAX_OSC72 - 3)
        self.assertEqual(parser.feed(body), b"")
        self.assertLessEqual(len(parser._held), MAX_OSC72 + 3)
        suffix = b"x" + paste(b"nested") + b"\x1b\\" + osc72() + paste(b"later")
        self.assertEqual(parser.feed(suffix), prefix + body + suffix)
        self.assertTrue(parser.disabled)
        self.assertEqual(len(parser._held), 0)
        self.assertEqual(calls, [])
        self.assertEqual(parser.finish(), b"")

    def test_incomplete_packet_prefixes_flush_without_handler_call(self):
        content = osc72()
        for offset in range(1, len(content)):
            with self.subTest(offset=offset):
                self.assert_input(content[:offset], content[:offset])

    def test_handler_exception_or_non_bool_non_bytes_result_disables_and_preserves(
        self,
    ):
        content = osc72() + paste(b"later")
        for result in (None, 0, 1, "not bytes", bytearray(b"not bytes")):
            with self.subTest(result=result):
                self.assert_input(
                    content,
                    content,
                    packets=(b"72;i=synthetic;encoded",),
                    packet_result=result,
                    disabled=True,
                )
        calls = []

        def broken(value):
            calls.append(value)
            raise RuntimeError("synthetic callback failure")

        parser = PasteInput(
            lambda value: self.fail("unexpected paste"), on_osc72=broken
        )
        self.assertEqual(parser.feed(content) + parser.finish(), content)
        self.assertEqual(calls, [b"72;i=synthetic;encoded"])
        self.assertTrue(parser.disabled)


class ReservedOSC72InputTests(FramingCase):
    def test_reserved_false_consumes_private_packet_but_other_osc_is_byte_exact(self):
        ordinary = b"\x1b]52;c;clipboard\x07\x1b]0;title\x1b\\"
        content = ordinary + osc72(b"i=unknown;denied") + paste(b"approved")
        self.assert_input(
            content,
            ordinary + paste(REPLACEMENT),
            packets=(b"72;i=unknown;denied",),
            payloads=(b"approved",),
            packet_result=False,
            reserve_osc72=True,
        )

    def test_reserved_bytes_replacement_still_precedes_enter(self):
        self.assert_input(
            osc72() + b"\r",
            paste(REPLACEMENT) + b"\r",
            packets=(b"72;i=synthetic;encoded",),
            packet_result=paste(REPLACEMENT),
            reserve_osc72=True,
        )

    def test_oversized_private_packet_discards_to_terminator_and_reservation_survives(
        self,
    ):
        for intro in OSC_INTROS:
            for ending in OSC_ENDS:
                for width in (1, 17, 4096, MAX_OSC72 * 2):
                    with self.subTest(intro=intro, ending=ending, width=width):
                        calls = []
                        parser = PasteInput(
                            lambda value: calls.append(("paste", value)) or REPLACEMENT,
                            on_osc72=lambda value: (
                                calls.append(("packet", value)) or False
                            ),
                            reserve_osc72=True,
                        )
                        private = osc72(b"x" * MAX_OSC72, intro=intro, end=ending)
                        ordinary = b"\x1b]52;c;keep\x07"
                        later = paste(b"no-new-authorization")
                        content = (
                            b"before" + private + ordinary + osc72() + later + b"after"
                        )
                        observed = bytearray()
                        for offset in range(0, len(content), width):
                            observed.extend(
                                parser.feed(content[offset : offset + width])
                            )
                            self.assertLessEqual(len(parser._held), MAX_OSC72 + 3)
                        self.assertTrue(parser.disabled)
                        observed.extend(parser.finish())
                        self.assertEqual(
                            bytes(observed), b"before" + ordinary + later + b"after"
                        )
                        self.assertEqual(calls, [])

    def test_malformed_private_payload_never_reaches_child_or_any_callback(self):
        malformed = {
            "nul": b"data\x00",
            "del": b"data\x7f",
            "newline": b"data\n",
            "raw_c1": b"data\x85",
            "encoded_c1": b"data\xc2\x85",
            "invalid_encoding": b"data\xff",
            "invalid_utf8_before_escape": b"data\xc3\x1b[31m",
            "overlong_st_continuation": b"data\xc0\x9cstill-private",
            "invalid_range_st_continuation": b"data\xe0\x9c\x9bstill-private",
            "surrogate_st_continuation": b"data\xed\xa0\x9cstill-private",
            "outside_unicode_st_continuation": b"data\xf4\x90\x9c\x9bstill-private",
            "escape_csi": b"data\x1b[31m",
            "escape_escape": b"data\x1b\x1b",
        }
        for name, payload in malformed.items():
            with self.subTest(case=name):
                later = paste(b"no-new-authorization")
                content = b"before" + osc72(payload) + osc72() + later + b"after"
                self.assert_input(
                    content,
                    b"before" + later + b"after",
                    disabled=True,
                    reserve_osc72=True,
                )
        for ending in (b"\x1b\x07", b"\x1b\x9c", b"\x1b\xc2\x9c"):
            with self.subTest(ending=ending):
                self.assert_input(
                    b"before\x1b]72;data" + ending + b"after",
                    b"beforeafter",
                    disabled=True,
                    reserve_osc72=True,
                )

    def test_partial_identifiable_private_packet_at_eof_is_discarded_not_flushed(self):
        for intro in OSC_INTROS:
            complete = osc72(intro=intro)
            for offset in range(len(intro) + 2, len(complete)):
                with self.subTest(intro=intro, offset=offset):
                    self.assert_input(
                        b"before" + complete[:offset],
                        b"before",
                        reserve_osc72=True,
                    )
        self.assert_input(b"before\x1b]7", b"before\x1b]7", reserve_osc72=True)

    def test_unterminated_oversized_private_input_retains_no_payload_at_finish(self):
        parser = PasteInput(
            lambda value: self.fail("unexpected paste"),
            on_osc72=lambda value: self.fail("unexpected packet"),
            reserve_osc72=True,
        )
        self.assertEqual(parser.feed(b"\x1b]72;"), b"")
        for _ in range(32):
            self.assertEqual(parser.feed(b"x" * 8192), b"")
            self.assertLessEqual(len(parser._held), MAX_OSC72 + 3)
        self.assertTrue(parser.disabled)
        self.assertEqual(parser.feed(b"\xc3"), b"")
        self.assertEqual(parser.finish(), b"")

    def test_invalid_callback_result_is_consumed_and_cannot_rearm_authorization(self):
        for result in (None, 0, 1, "not bytes", bytearray(b"not bytes")):
            with self.subTest(result=result):
                later = paste(b"not-authorized")
                self.assert_input(
                    osc72() + osc72() + later,
                    later,
                    packets=(b"72;i=synthetic;encoded",),
                    packet_result=result,
                    disabled=True,
                    reserve_osc72=True,
                )

    def test_prior_nonprivate_encoding_failure_does_not_disable_packet_reservation(
        self,
    ):
        later = paste(b"not-authorized")
        content = b"\xff" + osc72() + later + b"\x1b]0;ordinary\x07"
        self.assert_input(
            content,
            b"\xff" + later + b"\x1b]0;ordinary\x07",
            disabled=True,
            reserve_osc72=True,
        )

    def test_reservation_after_failure_still_keeps_other_control_strings_opaque(self):
        opaque = b"\x1bPdata" + osc72(end=b"\x07") + b"\x1b\\"
        self.assert_input(
            b"\xff" + opaque + osc72(),
            b"\xff" + opaque,
            disabled=True,
            reserve_osc72=True,
        )

    def test_reserved_mode_without_handler_consumes_protocol_but_can_transform_paths(
        self,
    ):
        self.assert_input(
            osc72() + paste(b"approved"),
            paste(REPLACEMENT),
            payloads=(b"approved",),
            packet_handler=False,
            reserve_osc72=True,
        )

    def test_malformed_reserved_selector_disables_but_never_leaks(self):
        for intro in OSC_INTROS:
            for ending in OSC_ENDS:
                with self.subTest(intro=intro, ending=ending):
                    later = paste(b"not-authorized")
                    self.assert_input(
                        intro + b"72" + ending + osc72() + later,
                        later,
                        disabled=True,
                        reserve_osc72=True,
                    )

    def test_reservation_flag_requires_an_actual_boolean(self):
        for value in (None, 0, 1, "true"):
            with self.subTest(value=value):
                with self.assertRaises(TypeError):
                    PasteInput(lambda payload: payload, reserve_osc72=value)


class OutputFilterTests(FramingCase):
    def test_only_reserved_protocol_is_removed_among_other_terminal_output(self):
        surrounding = (
            b"text\x1b[31mred\x1b[0m\x1b]0;title\x07"
            b"\x1b]52;c;clipboard\x1b\\"
            b"\x1b]133;A\x07\x1b[?2004h"
        )
        self.assert_output(
            surrounding + osc72() + b"after", surrounding + NEUTRAL + b"after"
        )

    def test_all_osc_introducers_and_terminators_are_blocked(self):
        for intro in OSC_INTROS:
            for ending in OSC_ENDS:
                with self.subTest(intro=intro, ending=ending):
                    content = b"before" + osc72(intro=intro, end=ending) + b"after"
                    # Kitty only recognizes C0 strings/terminators. Keep all
                    # old C1 vectors: their introducers are ordinary output,
                    # and C1 ST cannot end a reserved literal C0 OSC packet.
                    expected = content
                    if intro == b"\x1b]":
                        expected = b"before" + NEUTRAL
                        if ending in (b"\x1b\\", b"\x07"):
                            expected += b"after"
                    self.assert_output(content, expected)

    def test_unknown_empty_and_malformed_protocol_bodies_never_reach_terminal(self):
        payloads = (
            b"",
            b"unknown",
            b"i=other;ask-for-disallowed-image",
            b"malformed;;;=!",
            b"\x00\xff\xc0\xaf",
            b"\xc0\x9cstill-private",
            b"\xe0\x9c\x9bstill-private",
            b"\xed\xa0\x9cstill-private",
            b"\xf4\x90\x9c\x9bstill-private",
            b"\x1b[200~/host/fake.png\x1b[201~",
            b"\x1b]0;nested\x1b[31m",
            b"\x1b\x1b[unexpected",
        )
        for payload in payloads:
            with self.subTest(payload=payload):
                self.assert_output(
                    b"before" + osc72(payload) + b"after",
                    b"before" + NEUTRAL + b"after",
                )

    def test_reserved_selector_without_separator_is_not_leaked_at_terminator_or_eof(
        self,
    ):
        for intro in OSC_INTROS:
            for ending in (*OSC_ENDS, b""):
                with self.subTest(intro=intro, ending=ending):
                    content = b"before" + intro + b"72" + ending
                    expected = b"before" + NEUTRAL if intro == b"\x1b]" else content
                    self.assert_output(content, expected)

    def test_neighbor_selectors_and_other_utf8_output_remain_byte_exact(self):
        for selector in (b"7;", b"720;", b"73;", b"072;", b"72x;", "７２;".encode()):
            with self.subTest(selector=selector):
                prefix = "ÜÛÝÞß".encode()
                content = prefix + b"\x1b]" + selector + b"data\x07"
                # Kitty v0.50.0 dispatch_osc() parses a decimal selector and
                # accepts a missing semicolon. These retained entries are
                # numeric OSC 72, unlike unrelated neighbor selectors.
                expected = (
                    prefix + NEUTRAL if selector in (b"072;", b"72x;") else content
                )
                self.assert_output(content, expected)

    def test_kitty_graphics_apc_and_extended_keyboard_csi_pass_byte_exact(self):
        content = (
            b"\x1b_Ga=T,f=100,i=42,m=0;AAECAwQFBgcICQ==\x1b\\"
            b"\x1b[97;5u\x1b[57358;1:2u\x1b[1;5A"
            b"\x9b97;5u\xc2\x9b97;5u"
        )
        self.assert_output(content, content)
        self.assert_output(content + osc72() + content, content + NEUTRAL + content)

    def test_other_control_strings_remain_opaque_even_with_nested_packet_bytes(self):
        for kind in ("dcs", "sos", "pm", "apc"):
            for intro in STRING_INTROS[kind]:
                for ending in STRING_ENDS:
                    with self.subTest(kind=kind, intro=intro, ending=ending):
                        opaque = intro + b"reply" + osc72(end=b"\x07") + ending
                        # The narrowed output contract reserves the literal C0
                        # private pattern even under a real or C1-only wrapper.
                        expected = intro + b"reply" + NEUTRAL + ending + NEUTRAL
                        self.assert_output(opaque + osc72(), expected)

    def test_opaque_strings_do_not_end_on_interior_escape_or_bel(self):
        for kind in ("dcs", "sos", "pm", "apc"):
            with self.subTest(kind=kind):
                opaque = (
                    STRING_INTROS[kind][0]
                    + b"data\x07\x1b[1;2R\x1b]"
                    + osc72(end=b"\x07")
                    + b"\x1b\\"
                )
                expected = (
                    STRING_INTROS[kind][0]
                    + b"data\x07\x1b[1;2R\x1b]"
                    + NEUTRAL
                    + b"\x1b\\"
                    + NEUTRAL
                )
                self.assert_output(opaque + osc72(), expected)

    def test_utf8_continuations_do_not_end_a_blocked_packet(self):
        payload = "ÜÛÝÞß".encode() + b"\xe2\x82\x9c\xf0\x9f\x98\x9b" + b"still-private"
        self.assert_output(osc72(payload) + b"public", NEUTRAL + b"public")

    def test_utf8_continuations_do_not_start_packets_or_close_other_strings(self):
        letters = "ÜÛÝÞß".encode()
        self.assert_output(
            letters + b"72;not-a-packet\x07", letters + b"72;not-a-packet\x07"
        )
        opaque = b"\x1bP" + letters + osc72(end=b"\x07") + b"\x1b\\"
        self.assert_output(opaque, b"\x1bP" + letters + NEUTRAL + b"\x1b\\")

    def test_malformed_encoding_cannot_hide_a_following_actual_packet(self):
        for prefix in (b"\xc3", b"\xc0", b"\xe0\x80", b"\xed\xa0", b"\xff"):
            for intro in OSC_INTROS:
                with self.subTest(prefix=prefix, intro=intro):
                    content = prefix + osc72(intro=intro) + b"after"
                    if prefix == b"\xc3" and intro == b"\x9d":
                        # C3 9D is valid UTF-8 U+00DD, not a malformed prefix
                        # followed by raw C1 OSC. Keep this matrix entry and all
                        # its fragmentations, preserving the original bytes.
                        self.assertEqual((prefix + intro).decode("utf-8"), "\u00dd")
                        boundary = OutputFilter()
                        # Ordinary UTF-8 bytes now stream immediately; the
                        # emitted-stream monitor still fences partial input.
                        self.assertEqual(boundary.feed(prefix), prefix)
                        self.assertFalse(boundary.safe_to_inject())
                        self.assertEqual(boundary.feed(intro), intro)
                        self.assertTrue(boundary.safe_to_inject())
                        self.assertEqual(boundary.finish(), b"")
                        self.assert_output(content, content)
                    else:
                        expected = (
                            prefix + NEUTRAL + b"after"
                            if intro == b"\x1b]"
                            else content
                        )
                        self.assert_output(content, expected)

    def test_malformed_prefixes_and_ordinary_data_are_otherwise_preserved(self):
        samples = (
            b"\xff\xc0\xaf ordinary",
            b"\xe0\x80\x80",
            b"\xed\xa0\x80",
            b"\xf4\x90\x80\x80",
            b"\x1b[2\x03bad",
            b"\x1b\x1b7",
            b"\x1b(B",
            b"\x1b]0;title\xff\xc3\x1b\\",
        )
        for content in samples:
            with self.subTest(content=content):
                self.assert_output(content, content)

    def test_output_bracket_markers_do_not_authorize_or_exempt_protocol_packets(self):
        content = START + osc72() + END
        self.assert_output(content, START + NEUTRAL + END)

    def test_unterminated_target_body_is_discarded_with_constant_storage(self):
        parser = OutputFilter()
        self.assertEqual(parser.feed(b"before\x1b]72;"), b"before")
        # A private packet is also bounded on raw wire bytes. Preserve the old
        # long-stream vector, but fail closed rather than continue indefinitely.
        for _ in range((MAX_OUTPUT_PRIVATE - 5) // 8192):
            self.assertEqual(parser.feed(b"x" * 8192), b"")
            self.assertLessEqual(len(parser._held), 7)
            self.assertLessEqual(len(parser._units.pending), 3)
        self.assertEqual(parser.feed(b"\xc3"), b"")
        with self.assertRaises(ReservationError):
            parser.feed(b"x" * (128 * 8192))
        self.assertFalse(parser.safe_to_inject())
        with self.assertRaises(ReservationError):
            parser.finish()
        with self.assertRaises(ReservationError):
            parser.finish()

    def test_non_target_body_is_forwarded_with_constant_storage(self):
        # A normal clipboard integration may exceed 64 KiB. It stays byte
        # exact below the ordinary-control limit, not the private-packet cap.
        clipboard = b"\x1b]52;c;" + b"A" * (80 * 1024) + b"\x1b\\"
        clipboard_parser = OutputFilter()
        self.assertEqual(clipboard_parser.feed(clipboard), clipboard)
        self.assertTrue(clipboard_parser.safe_to_inject())
        self.assertEqual(clipboard_parser.finish(), b"")
        for prefix in (b"\x1b]0;", b"\x1bP", b"\x1bX", b"\x1b^", b"\x1b_G"):
            with self.subTest(prefix=prefix):
                parser = OutputFilter()
                self.assertEqual(parser.feed(prefix), prefix)
                for _ in range((MAX_OUTPUT_CONTROL - len(prefix)) // 8192):
                    content = b"x" * 8192
                    self.assertEqual(parser.feed(content), content)
                    self.assertLessEqual(len(parser._held), 2)
                with self.assertRaises(ReservationError):
                    parser.feed(b"x" * (32 * 8192))
                with self.assertRaises(ReservationError):
                    parser.feed(b"\x1b\\")
                with self.assertRaises(ReservationError):
                    parser.finish()

    def test_incomplete_non_reserved_frames_flush_without_loss(self):
        for content in (
            b"\x1b",
            b"\x1b[",
            b"\x1b[20",
            b"\x1b]",
            b"\x1b]7",
            b"\x1bPdata\x1b",
            b"\xc3",
            b"\xe2\x82",
            b"\xf0\x9f\x98",
        ):
            with self.subTest(content=content):
                self.assert_output(content, content)

    def test_output_filter_finish_is_idempotent_and_seals_stream(self):
        parser = OutputFilter()
        self.assertEqual(parser.feed(b"\x1b]72;unfinished"), b"")
        self.assertEqual(parser.finish(), NEUTRAL)
        self.assertEqual(parser.finish(), b"")
        with self.assertRaises(ValueError):
            parser.feed(b"cannot bypass by reusing a finished stream")

    def test_safe_to_inject_only_at_top_level_text_with_no_partial_encoding(self):
        cases = {
            "escape": (b"\x1b", b"7"),
            "escape_intermediate": (b"\x1b(", b"B"),
            "csi": (b"\x1b[97;5", b"u"),
            "raw_c1_csi": (b"\x9b97;5", b"u"),
            "utf8_c1_csi": (b"\xc2\x9b97;5", b"u"),
            "osc": (b"\x1b]0;title", b"\x07"),
            "osc_terminator_prefix": (b"\x1b]0;title\x1b", b"\\"),
            "reserved_osc": (b"\x1b]72;private", b"\x1b\\"),
            "raw_c1_osc": (b"\x9d0;title", b"\x9c"),
            "utf8_c1_osc": (b"\xc2\x9d0;title", b"\xc2\x9c"),
            "kitty_graphics_apc": (b"\x1b_Ga=T,f=100;AAECAw==", b"\x1b\\"),
            "kitty_graphics_st_prefix": (b"\x1b_Ga=T;AAEC\x1b", b"\\"),
            "dcs": (b"\x1bPdata\x07\x1b[1;2R", b"\x1b\\"),
            "sos": (b"\x1bXdata", b"\x1b\\"),
            "pm": (b"\x1b^data", b"\x1b\\"),
            "utf8_two_bytes": (b"\xc3", b"\x9c"),
            "utf8_three_bytes": (b"\xe2\x82", b"\x9c"),
            "utf8_four_bytes": (b"\xf0\x9f\x98", b"\x9b"),
        }
        for name, (prefix, suffix) in cases.items():
            with self.subTest(case=name):
                parser = OutputFilter()
                self.assertTrue(parser.safe_to_inject())
                observed = parser.feed(prefix)
                # Kitty's shared string terminator ends the retained DCS
                # prefix at BEL; its following complete CSI is top-level.
                # Raw/UTF-8 C1 no longer opens output control contexts in this
                # Kitty-only view. Host-input C1 opacity remains unchanged.
                self.assertEqual(
                    parser.safe_to_inject(),
                    name
                    in (
                        "dcs",
                        "raw_c1_csi",
                        "utf8_c1_csi",
                        "raw_c1_osc",
                        "utf8_c1_osc",
                    ),
                )
                observed += parser.feed(suffix)
                self.assertTrue(parser.safe_to_inject())
                expected = NEUTRAL if name == "reserved_osc" else prefix + suffix
                self.assertEqual(observed, expected)
                self.assertEqual(parser.finish(), b"")
                self.assertFalse(parser.safe_to_inject())

    def test_safe_to_inject_stays_false_across_every_native_graphics_byte(self):
        graphics = b"\x1b_Ga=T,f=100,i=42,m=0;AAECAwQFBgcICQ==\x1b\\"
        parser = OutputFilter()
        observed = bytearray()
        for offset, value in enumerate(graphics):
            observed.extend(parser.feed(bytes((value,))))
            self.assertEqual(parser.safe_to_inject(), offset == len(graphics) - 1)
        self.assertEqual(bytes(observed), graphics)
        self.assertTrue(parser.safe_to_inject())
        self.assertEqual(parser.feed(b"\x1b[97;5u"), b"\x1b[97;5u")
        self.assertTrue(parser.safe_to_inject())
        self.assertEqual(parser.finish(), b"")


class HostDropSecurityRegressions(FramingCase):
    """Source-grounded HD-01/02/03 cases from Kitty v0.50.0 vt-parser.c.

    dispatch_osc() reads at most five decimal digits and optionally skips ';'.
    find_st_terminator() accepts BEL for every output control-string kind.
    Input authorization deliberately retains its stronger opaque-through-ST
    rule for non-OSC replies, even after callbacks have been disabled.
    """

    def test_numeric_72_aliases_and_missing_separators_are_reserved_on_output(self):
        selectors = (
            b"72;",
            b"072;",
            b"0072;",
            b"00072;",
            b"72",
            b"072",
            b"72x;",
            b"000720;",
            b"000729999999999999999;",
        )
        for intro in OSC_INTROS:
            for ending in OSC_ENDS:
                for selector in selectors:
                    with self.subTest(intro=intro, ending=ending, selector=selector):
                        content = (
                            b"before" + intro + selector + b"t=A;" + ending + b"after"
                        )
                        expected = content
                        if intro == b"\x1b]":
                            expected = b"before" + NEUTRAL
                            if ending in (b"\x1b\\", b"\x07"):
                                expected += b"after"
                        self.assert_output(content, expected)

    def test_five_digit_selector_limit_does_not_invent_integer_wrapping(self):
        # Five leading digits are the complete bounded accumulator in the
        # inspected Kitty implementation, not an arbitrary-width OSC integer.
        selectors = (
            b"000072;",
            b"00000072;",
            b"4294967368;",
            b"18446744073709551688;",
            b"720;",
            b"72000t=A;",
            b"73;",
            b"52;",
            b"+72;",
            b"-72;",
            b" 72;",
        )
        for selector in selectors:
            with self.subTest(selector=selector):
                content = b"\x1b]" + selector + b"ordinary\x07"
                self.assert_output(content, content)
                self.assert_input(content, content, reserve_osc72=True)

    def test_malformed_selector_separator_cannot_flush_reserved_payload(self):
        for selector in (b"72", b"072", b"0072", b"00072"):
            for separator in (
                b"\xff",
                b"\xc0\xaf",
                b"\xe0\x80\x80",
                b"\x00",
                b"\x1b[31m",
            ):
                with self.subTest(selector=selector, separator=separator):
                    content = b"before\x1b]" + selector + separator + b"t=A;\x1b\\after"
                    self.assert_output(content, b"before" + NEUTRAL + b"after")
                    self.assert_input(
                        content,
                        b"beforeafter",
                        disabled=True,
                        reserve_osc72=True,
                    )

    def test_selector_and_body_storage_are_bounded_for_long_zero_runs(self):
        for private in (False, True):
            for input_direction in (False, True):
                with self.subTest(private=private, input_direction=input_direction):
                    calls = []
                    parser = (
                        PasteInput(
                            lambda value: calls.append(("paste", value)) or REPLACEMENT,
                            on_osc72=lambda value: (
                                calls.append(("packet", value)) or True
                            ),
                            reserve_osc72=True,
                        )
                        if input_direction
                        else OutputFilter()
                    )
                    prefix = b"\x1b]00072" if private else b"\x1b]00000"
                    observed = bytearray(parser.feed(prefix))
                    if not input_direction:
                        limit = MAX_OUTPUT_PRIVATE if private else MAX_OUTPUT_CONTROL
                        for _ in range((limit - len(prefix)) // 8192):
                            observed.extend(parser.feed(b"0" * 8192))
                            self.assertLessEqual(len(parser._held), 7)
                            self.assertLessEqual(len(parser._units.pending), 3)
                        # The retained 32*8192 vector exceeds the new target
                        # recovery guard; only input may continue streaming it.
                        with self.assertRaises(ReservationError):
                            parser.feed(b"0" * (32 * 8192))
                        with self.assertRaises(ReservationError):
                            parser.finish()
                        self.assertEqual(calls, [])
                        continue
                    for _ in range(32):
                        body = b"0" * 8192
                        observed.extend(parser.feed(body))
                        self.assertLessEqual(len(parser._held), 7)
                        self.assertLessEqual(len(parser._units.pending), 3)
                    observed.extend(parser.feed(b";t=A;\x1b\\"))
                    observed.extend(parser.finish())
                    expected = (
                        b"" if private else prefix + b"0" * (32 * 8192) + b";t=A;\x1b\\"
                    )
                    self.assertEqual(bytes(observed), expected)
                    self.assertEqual(calls, [])

    def test_numeric_input_aliases_are_discarded_not_authorization_packets(self):
        for selector in (b"072;", b"0072;", b"00072;", b"72t=", b"72x;"):
            for intro in OSC_INTROS:
                for ending in OSC_ENDS:
                    with self.subTest(selector=selector, intro=intro, ending=ending):
                        later = paste(b"not-authorized")
                        content = b"before" + intro + selector + b"A;" + ending + later
                        self.assert_input(
                            content,
                            b"before" + later,
                            disabled=True,
                            reserve_osc72=True,
                        )

    def test_partial_numeric_72_selectors_are_reserved_at_eof(self):
        for intro in OSC_INTROS:
            for selector in (b"72", b"072", b"0072", b"00072", b"000720;t=A;"):
                for fragmentation, chunks in fragmentations(intro + selector):
                    with self.subTest(
                        intro=intro, selector=selector, fragmentation=fragmentation
                    ):
                        for parser in (
                            OutputFilter(),
                            PasteInput(
                                lambda value: self.fail("unexpected paste"),
                                on_osc72=lambda value: self.fail("unexpected packet"),
                                reserve_osc72=True,
                            ),
                        ):
                            observed = b"".join(parser.feed(chunk) for chunk in chunks)
                            self.assertLessEqual(len(parser._held), 7)
                            expected = b""
                            if isinstance(parser, OutputFilter):
                                expected = (
                                    NEUTRAL if intro == b"\x1b]" else intro + selector
                                )
                            self.assertEqual(observed + parser.finish(), expected)
        for selector in (b"0", b"00", b"7", b"00007", b"00000"):
            content = b"\x1b]" + selector
            with self.subTest(nonprivate=selector):
                self.assert_output(content, content)
                self.assert_input(content, content, reserve_osc72=True)

    def test_kitty_bel_ends_output_strings_before_a_following_private_packet(self):
        for kind in ("dcs", "sos", "pm", "apc"):
            for intro in STRING_INTROS[kind]:
                with self.subTest(kind=kind, intro=intro):
                    prefix = intro + b"ignored\x07"
                    content = prefix + osc72(b"t=A;", end=b"\x07") + b"\x1b\\"
                    self.assert_output(content, prefix + NEUTRAL + b"\x1b\\")

    def test_output_bel_boundary_keeps_graphics_and_keyboard_bytes_exact(self):
        for kind in ("dcs", "sos", "pm", "apc"):
            with self.subTest(kind=kind):
                parser = OutputFilter()
                prefix = STRING_INTROS[kind][0] + b"ignored"
                self.assertEqual(parser.feed(prefix), prefix)
                self.assertFalse(parser.safe_to_inject())
                self.assertEqual(parser.feed(b"\x07"), b"\x07")
                self.assertTrue(parser.safe_to_inject())
                self.assertEqual(parser.feed(osc72(b"t=A;")), NEUTRAL)
                self.assertTrue(parser.safe_to_inject())
                keyboard = b"\x1b[97;5u\x1b[57358;1:2u"
                self.assertEqual(parser.feed(keyboard), keyboard)
                self.assertEqual(parser.finish(), b"")
        graphics = b"\x1b_Ga=T,f=100;AAECAw==\x07"
        self.assert_output(graphics + osc72(b"t=A;"), graphics + NEUTRAL)

    def test_input_bel_opacity_does_not_change_after_callbacks_are_disabled(self):
        for failure in (b"", b"\xff"):
            for kind in ("dcs", "sos", "pm", "apc"):
                with self.subTest(failure=failure, kind=kind):
                    opaque = (
                        STRING_INTROS[kind][0]
                        + b"reply\x07"
                        + paste(b"never-authorized")
                        + osc72(end=b"\x07")
                        + b"\x1b\\"
                    )
                    self.assert_input(
                        failure + opaque + osc72(),
                        failure + opaque,
                        packets=() if failure else (b"72;i=synthetic;encoded",),
                        packet_result=True,
                        disabled=bool(failure),
                        reserve_osc72=True,
                    )

    def test_malformed_paste_carries_the_held_escape_into_private_reservation(self):
        for intro in OSC_INTROS:
            for ending in OSC_ENDS:
                with self.subTest(intro=intro, ending=ending):
                    prefix = START + b"oops"
                    private = osc72(b"t=r:i=1;U1lOVEhFVElD", intro=intro, end=ending)
                    self.assert_input(
                        prefix + private + b"\rafter",
                        prefix + b"\rafter",
                        disabled=True,
                        reserve_osc72=True,
                    )

    def test_partial_paste_end_marker_recovery_keeps_framing_not_authority(self):
        for count in range(1, len(END)):
            with self.subTest(count=count):
                prefix = START + b"oops" + END[:count]
                later = paste(b"never-rearm")
                self.assert_input(
                    prefix + osc72() + later,
                    prefix + later,
                    disabled=True,
                    reserve_osc72=True,
                )

    def test_private_packet_after_malformed_paste_never_flushes_at_partial_eof(self):
        prefix = START + b"oops"
        private = osc72(b"t=r:i=1;U1lOVEhFVElD")
        for offset in range(4, len(private)):
            with self.subTest(offset=offset):
                self.assert_input(
                    prefix + private[:offset],
                    prefix,
                    disabled=True,
                    reserve_osc72=True,
                )
        parser = PasteInput(
            lambda value: self.fail("unexpected paste"),
            on_osc72=lambda value: self.fail("unexpected packet"),
            reserve_osc72=True,
        )
        self.assertEqual(parser.feed(prefix + b"\x1b]72;"), prefix)
        for _ in range(32):
            self.assertEqual(parser.feed(b"x" * 8192), b"")
            self.assertLessEqual(len(parser._held), 7)
        self.assertTrue(parser.disabled)
        self.assertEqual(parser.finish(), b"")

    def test_malformed_paste_recovery_keeps_input_direction_and_callbacks_disabled(
        self,
    ):
        prefix = START + b"oops"
        opaque = (
            b"\x1bPignored\x07"
            + paste(b"never-authorized")
            + osc72(end=b"\x07")
            + b"\x1b\\"
        )
        later = paste(b"never-rearm")
        self.assert_input(
            prefix + osc72() + opaque + osc72() + later,
            prefix + opaque + later,
            disabled=True,
            reserve_osc72=True,
        )


class KittyWireReservationTests(FramingCase):
    """HD-05: literal C0 reservation is not generic C0/C1 terminal emulation."""

    def test_c0_private_wire_is_neutralized_under_c0_and_c1_wrappers(self):
        for kind in ("osc", "dcs", "sos", "pm", "apc"):
            for intro in STRING_INTROS[kind]:
                with self.subTest(kind=kind, intro=intro):
                    content = intro + b"reply" + osc72(b"t=A;", end=b"\x07") + b"\x1b\\"
                    expected = intro + b"reply" + NEUTRAL + b"\x1b\\"
                    self.assert_output(content, expected)

    def test_neutralization_cannot_join_outer_c0_or_c1_numeric_selectors(self):
        for prefix in (b"\x1b]7", b"\x9d7", b"\xc2\x9d7"):
            for selector in (b"72;", b"072;", b"0072;", b"00072;", b"72"):
                with self.subTest(prefix=prefix, selector=selector):
                    private = b"\x1b]" + selector + b"t=A;\x1b\\"
                    suffix = b"2t=A;\x07"
                    content = prefix + private + suffix
                    expected = prefix + NEUTRAL + suffix
                    self.assertEqual(private_c0_offsets(expected), [])
                    self.assert_output(content, expected)

    def test_every_emitted_fragment_remains_free_of_private_c0_headers(self):
        content = (
            b"\xc2\x9d7"
            + osc72(b"t=A;")
            + b"2t=A;\x07"
            + b"\x1b]7"
            + osc72(b"t=r:i=1;U1lOVEhFVElD")
            + b"2t=A;\x07"
        )
        expected = (
            b"\xc2\x9d7" + NEUTRAL + b"2t=A;\x07" + b"\x1b]7" + NEUTRAL + b"2t=A;\x07"
        )
        for fragmentation, chunks in fragmentations(content):
            with self.subTest(fragmentation=fragmentation):
                parser = OutputFilter()
                observed = bytearray()
                for chunk in chunks:
                    observed.extend(parser.feed(chunk))
                    self.assertEqual(private_c0_offsets(bytes(observed)), [])
                observed.extend(parser.finish())
                self.assertEqual(bytes(observed), expected)
                self.assertEqual(private_c0_offsets(bytes(observed)), [])

    def test_injection_boundary_uses_neutralized_emission_not_original_wrapper(self):
        for prefix in (b"\x1bPignored", b"\x1b_GAAAA", b"\x1b]7"):
            with self.subTest(prefix=prefix):
                parser = OutputFilter()
                observed = parser.feed(prefix)
                self.assertFalse(parser.safe_to_inject())
                observed += parser.feed(osc72(b"t=A;"))
                self.assertEqual(observed, prefix + NEUTRAL)
                # The replacement closed the actual emitted C0 string, even
                # though the original outer stream has not supplied its ST.
                self.assertTrue(parser.safe_to_inject())
                self.assertEqual(parser.finish(), b"")

    def test_unknown_escape_byte_is_consumed_without_generic_escape_restart(self):
        for content in (
            b"\x1b\x00",
            b"\x1b\x1b]ordinary\x07",
            b"\x1b(\x1b",
            b"\x1b \x00",
        ):
            for fragmentation, chunks in fragmentations(content):
                with self.subTest(content=content, fragmentation=fragmentation):
                    parser = OutputFilter()
                    observed = b"".join(parser.feed(chunk) for chunk in chunks)
                    observed += parser.finish()
                    self.assertEqual(observed, content)
        parser = OutputFilter()
        self.assertEqual(parser.feed(b"\x1b\x00"), b"\x1b\x00")
        self.assertTrue(parser.safe_to_inject())
        self.assertEqual(parser.feed(b"\x1b"), b"")
        self.assertFalse(parser.safe_to_inject())
        self.assertEqual(parser.feed(b"\x1b"), b"\x1b")
        self.assertFalse(parser.safe_to_inject())
        self.assertEqual(parser.feed(b"]ordinary\x07"), b"\x1b]ordinary\x07")
        self.assertTrue(parser.safe_to_inject())
        self.assertEqual(parser.finish(), b"")

    def test_csi_invalid_order_recovery_tracks_the_next_real_graphics_context(self):
        for invalid in (b"\x1b[1?", b"\x1b[1 0", b"\x1b[1\x00"):
            prefix = invalid + b"\x1b_Ga=T,f=100;AAECAw=="
            for fragmentation, chunks in fragmentations(prefix):
                with self.subTest(invalid=invalid, fragmentation=fragmentation):
                    parser = OutputFilter()
                    observed = b"".join(parser.feed(chunk) for chunk in chunks)
                    self.assertEqual(observed, prefix)
                    self.assertFalse(parser.safe_to_inject())
                    self.assertEqual(parser.feed(b"\x1b\\"), b"\x1b\\")
                    self.assertTrue(parser.safe_to_inject())
                    self.assertEqual(parser.finish(), b"")

    def test_csi_parameter_recovery_guard_refuses_before_target_boundary(self):
        parser = OutputFilter()
        self.assertEqual(parser.feed(b"\x1b["), b"\x1b[")
        with self.assertRaises(ReservationError):
            parser.feed(b"1;" * 65 + b"1m" + osc72(b"t=A;"))
        self.assertFalse(parser.safe_to_inject())
        with self.assertRaises(ReservationError):
            parser.feed(b"unfiltered fallback is forbidden")
        with self.assertRaises(ReservationError):
            parser.finish()

    def test_two_mebibyte_wrapper_is_refused_before_private_suffix(self):
        for intro in (b"\x1bP", b"\x1b_G", b"\x1b]0;", b"\x1b^", b"\x1bX"):
            content = intro + b"x" * (2 * 1024 * 1024) + osc72(b"t=A;")
            for width in (len(content), 17, 8192):
                with self.subTest(intro=intro, width=width):
                    parser = OutputFilter()
                    observed = bytearray()
                    refused = False
                    for offset in range(0, len(content), width):
                        try:
                            observed.extend(
                                parser.feed(content[offset : offset + width])
                            )
                        except ReservationError:
                            refused = True
                            break
                    self.assertTrue(refused)
                    self.assertLessEqual(len(observed), MAX_OUTPUT_CONTROL)
                    self.assertEqual(private_c0_offsets(bytes(observed)), [])
                    self.assertLessEqual(len(parser._held), 7)
                    self.assertFalse(parser.safe_to_inject())
                    with self.assertRaises(ReservationError):
                        parser.finish()

    def test_output_control_limit_counts_raw_utf8_bytes_including_partial_units(self):
        parser = OutputFilter()
        prefix = b"\x1bP" + b"x" * (MAX_OUTPUT_CONTROL - 3) + b"\xc3"
        self.assertEqual(len(prefix), MAX_OUTPUT_CONTROL)
        self.assertEqual(parser.feed(prefix), prefix)
        self.assertFalse(parser.safe_to_inject())
        with self.assertRaises(ReservationError):
            parser.feed(b"\x9c")
        with self.assertRaises(ReservationError):
            parser.finish()

    def test_private_discard_is_raw_bounded_without_large_capture(self):
        exact = b"\x1b]72;" + b"x" * (MAX_OUTPUT_PRIVATE - 7) + b"\x1b\\"
        parser = OutputFilter()
        self.assertEqual(parser.feed(exact), NEUTRAL)
        self.assertTrue(parser.safe_to_inject())
        self.assertEqual(parser.finish(), b"")
        parser = OutputFilter()
        self.assertEqual(parser.feed(b"\x1b]72;"), b"")
        self.assertEqual(parser.feed(b"x" * (MAX_OUTPUT_PRIVATE - 5)), b"")
        self.assertLessEqual(len(parser._held), 7)
        with self.assertRaises(ReservationError):
            parser.feed(b"\xc3")
        self.assertFalse(parser.safe_to_inject())
        with self.assertRaises(ReservationError):
            parser.feed(b"\x1b\\")

    def test_private_partial_eof_is_neutralized_but_input_removal_stays_empty(self):
        private = osc72(b"t=r:i=1;U1lOVEhFVElD")
        for offset in range(4, len(private)):
            with self.subTest(offset=offset):
                self.assert_output(private[:offset], NEUTRAL)
                self.assert_input(private[:offset], b"", reserve_osc72=True)

    def test_c1_only_output_is_byte_exact_without_a_false_opaque_context(self):
        for intros in STRING_INTROS.values():
            for intro in intros[1:]:
                for ending in OSC_ENDS:
                    with self.subTest(intro=intro, ending=ending):
                        content = intro + b"72;ordinary" + ending
                        self.assert_output(content, content)
                        parser = OutputFilter()
                        self.assertEqual(
                            parser.feed(intro + b"ordinary"), intro + b"ordinary"
                        )
                        self.assertTrue(parser.safe_to_inject())
                        self.assertEqual(parser.finish(), b"")


if __name__ == "__main__":
    unittest.main()
