import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tools.extract_surya_tables import expand_grid, main, parse_table


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


if __name__ == "__main__":
    unittest.main()
