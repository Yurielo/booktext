import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tools.extract_surya_tables import expand_grid, main, parse_table
import booktext


class SuryaTableExtractionTests(unittest.TestCase):
    def test_preserves_text_and_expands_spans(self):
        rows = parse_table(
            "<table><tr><th colspan='2'>Heading</th></tr>"
            "<tr><td rowspan='2'><b>Alpha</b></td><td>352.05</td></tr>"
            "<tr><td>353.50</td></tr></table>"
        )
        self.assertEqual(
            expand_grid(rows),
            [["Heading", ""], ["Alpha", "352.05"], ["", "353.50"]],
        )

    def test_cli_writes_portable_source_provenance(self):
        payload = {
            "page-0001": [{
                "blocks": [{
                    "label": "Table",
                    "confidence": 0.9,
                    "bbox": [1, 2, 3, 4],
                    "html": "<table><tr><td>Cell</td></tr></table>",
                }]
            }]
        }
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            results = root / "results.json"
            output = root / "output"
            results.write_text(json.dumps(payload), encoding="utf-8")
            with patch.object(
                sys, "argv", [
                    "extract_surya_tables.py", "--results", str(results),
                    "--output-dir", str(output), "--pages", "1,2",
                ]
            ):
                main()

            manifest = json.loads((output / "manifest.json").read_text())
            table = json.loads((output / "page-0001-table-01.json").read_text())

        self.assertEqual(manifest["artifact_type"], "booktext.surya_table_manifest")
        self.assertEqual(manifest["source"]["filename"], "results.json")
        self.assertNotIn(str(results), json.dumps(manifest))
        self.assertEqual(manifest["pages"][1]["status"], "MISSING")
        self.assertEqual(table["grid"], [["Cell"]])

    def test_image_only_and_explicit_figure_are_review_only(self):
        payload = {"page-0001": [{"blocks": [
            {"label": "Table", "html": "<img/>", "bbox": [1, 2, 3, 4]},
            {"label": "Figure", "html": "<img/>", "bbox": [5, 6, 7, 8], "table_context": True},
        ]}], "page-0002": [{"blocks": [
            {"label": "Figure", "html": "<img/>", "bbox": [1, 2, 3, 4]},
        ]}]}
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); results = root / "results.json"; output = root / "output"
            results.write_text(json.dumps(payload), encoding="utf-8")
            with patch.object(sys, "argv", ["x", "--results", str(results), "--output-dir", str(output), "--pages", "1,2"]):
                main()
            pages = json.loads((output / "manifest.json").read_text())["pages"]
        self.assertTrue(all(item["review_required"] for item in pages[0]["tables"]))
        self.assertEqual(pages[1]["status"], "NO_TABLE_BLOCK")

    def test_production_wrapper_returns_page_references_and_artifacts(self):
        payload = {"page-0313": [{"blocks": [
            {"label": "Table", "html": "<table><tr><td>A</td></tr></table>"},
            {"label": "Table", "html": "<table><tr><td>B</td></tr></table>"},
        ]}], "page-0319": [{"blocks": [{"label": "Table", "html": "<img/>"}]}]}
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); surya = root / "surya" / "surya-input"; surya.mkdir(parents=True)
            (surya / "results.json").write_text(json.dumps(payload), encoding="utf-8")
            references = booktext.extract_structured_tables(root / "surya", root / "tables", [313, 319])
            self.assertEqual(len(references[313]["tables"]), 2)
            self.assertEqual(references[319]["tables"][0]["status"], "fallback_review")
            self.assertTrue((root / "tables" / "page-0313-table-02.tsv").exists())


if __name__ == "__main__":
    unittest.main()
