import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "BookTextApp/Sources/BookTextApp/FinalBookView.swift"


class NativeExportUIContractTests(unittest.TestCase):
    def test_export_completion_does_not_open_external_application(self):
        source = SOURCE.read_text(encoding="utf-8")
        completion = re.search(r"private func exportCompleted.*?(?=\n    func openCompletedExport)", source, re.DOTALL)
        self.assertIsNotNone(completion)
        self.assertNotIn("NSWorkspace.shared.open", completion.group(0))
        self.assertNotIn("activateFileViewerSelecting", completion.group(0))
        self.assertIn('Button("Open exported file", action: model.openCompletedExport)', source)
        self.assertIn('Button("Reveal exported file", action: model.revealCompletedExport)', source)

    def test_external_opening_is_limited_to_explicit_user_actions(self):
        source = SOURCE.read_text(encoding="utf-8")
        self.assertRegex(source, r"func openCompletedExport\(\).*NSWorkspace\.shared\.open")
        self.assertRegex(source, r"func revealCompletedExport\(\).*activateFileViewerSelecting")

    def test_translation_export_is_explicit_and_reuses_selected_formats(self):
        source = SOURCE.read_text(encoding="utf-8")
        self.assertIn('Button("Export translation")', source)
        self.assertIn('call(["translation-export", outputURL.path', source)
        self.assertIn('formats: ExportFormatSelection.ordered(selectedFormats)', source)


if __name__ == "__main__":
    unittest.main()
