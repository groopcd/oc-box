import unittest

from oc_box import cli_table


class TableTests(unittest.TestCase):
    def test_cells_escape_terminal_controls_without_mangling_printable_text(self):
        rendered = cli_table.render(
            ("NAME", "VALUE"),
            [("wide 表", "first\nsecond\x1b[31m\u202e"), ("name", "café")],
        )
        self.assertIn(r"first\nsecond\u001b[31m\u202e", rendered)
        self.assertIn("café", rendered)
        self.assertNotIn("\x1b", rendered)
        self.assertNotIn("\u202e", rendered)
        self.assertEqual(len(rendered.splitlines()), 3)

    def test_property_table_preserves_zero_false_and_nested_list_items(self):
        report = {
            "metrics": {"count": 0, "running": False},
            "events": [{"name": "done"}],
        }
        lines = [line.split() for line in cli_table.fields(report).splitlines()]
        self.assertEqual(
            lines,
            [
                ["FIELD", "VALUE"],
                ["metrics.count", "0"],
                ["metrics.running", "no"],
                ["events[1].name", "done"],
            ],
        )

    def test_streaming_heading_can_be_omitted_without_changing_column_positions(self):
        headers = ("TIME", "LEVEL", "EVENT")
        first = cli_table.render(headers, [("1", "INFO", "first")], widths=(4, 5))
        following = cli_table.render(
            headers, [("2", "ERROR", "second")], widths=(4, 5), heading=False
        )
        self.assertEqual(
            first.splitlines()[1].index("first"), following.index("second")
        )
        self.assertNotIn("LEVEL", following)

    def test_field_table_bounds_wide_nested_data_before_padding(self):
        long_label = "field-" + "x" * 32768
        report = {long_label: [{"name": "y" * 8192}] * 10000}
        rendered = cli_table.fields(report)
        self.assertIn("[additional fields omitted]", rendered)
        self.assertLessEqual(len(rendered.splitlines()), cli_table._ROW_LIMIT + 2)
        self.assertLess(len(rendered), 700000)
        self.assertNotIn(long_label, rendered)
        self.assertNotIn("y" * 8192, rendered)

    def test_field_table_does_not_expand_deep_paths(self):
        nested = "leaf-below-depth-bound"
        for _ in range(32):
            nested = {"child": nested}
        rendered = cli_table.fields({"root": nested})
        self.assertIn("[nested fields omitted]", rendered)
        self.assertNotIn("leaf-below-depth-bound", rendered)
