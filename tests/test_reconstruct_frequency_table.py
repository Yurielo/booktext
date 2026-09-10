import unittest

from tools.reconstruct_frequency_table import (
    TableHTMLParser,
    VisionLine,
    align_names,
    canonical_number,
    make_cell,
)


class TableExtractorTests(unittest.TestCase):
    def test_parser_uses_body_rows_only(self):
        parser = TableHTMLParser()
        parser.feed(
            "<table><thead><tr><th>Heading</th></tr></thead>"
            "<tbody><tr><td>Campylobacter pyloridis</td><td>352.0</td>"
            "<td>357.2</td><td>355</td></tr></tbody></table>"
        )
        self.assertEqual(
            parser.tables,
            [[["Campylobacter pyloridis", "352.0", "357.2", "355"]]],
        )

    def test_number_comparison_accepts_ocr_separator_spacing(self):
        self.assertEqual(canonical_number("365 3"), canonical_number("365.3"))
        self.assertEqual(canonical_number("356,393"), "356.393")

    def test_agreement_and_disagreement_are_explicit(self):
        agreeing = VisionLine("352 0", 0.5, 0.8, 0.1, 0.02, 0.7)
        cell = make_cell("352.0", agreeing, "low", 0.9)
        self.assertFalse(cell["review_required"])
        self.assertEqual(cell["source"], "surya_vision_agreement")

        disagreeing = VisionLine("353.0", 0.5, 0.8, 0.1, 0.02, 0.7)
        cell = make_cell("352.0", disagreeing, "low", 0.9)
        self.assertTrue(cell["review_required"])
        self.assertEqual(cell["final"], "352.0")

    def test_alignment_can_join_wrapped_vision_name(self):
        rows = [["Cabbage Black Rot, черная капустная порча."]]
        lines = [
            VisionLine("Cabbage Black Rot, черная капустная", 0.08, 0.8, 0.3, 0.02, 0.5),
            VisionLine("порча.", 0.08, 0.78, 0.1, 0.02, 0.6),
        ]
        match = align_names(rows, lines)[0]
        self.assertIsNotNone(match)
        self.assertEqual(len(match), 2)


if __name__ == "__main__":
    unittest.main()
