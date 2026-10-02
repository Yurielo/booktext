import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
APP = (ROOT / "BookTextApp/Sources/BookTextApp/BookTextApp.swift").read_text(encoding="utf-8")
FINAL = (ROOT / "BookTextApp/Sources/BookTextApp/FinalBookView.swift").read_text(encoding="utf-8")
PRESENTATION = (ROOT / "BookTextApp/Sources/BookTextCore/DocumentPresentation.swift").read_text(encoding="utf-8")


class NativeUXV2ContractTests(unittest.TestCase):
    def test_main_hierarchy_is_document_centric(self):
        main = APP.split("struct SettingsView", 1)[0]
        for label in (
            'Text("Choose a document")',
            'Button("Choose File"',
            'Button("Start Processing"',
            'Button("Open Book")',
            'Button("Translate")',
            'Button("Export…")',
        ):
            self.assertIn(label, main)
        self.assertIn('Text("Recent Projects")', main)
        self.assertIn('Button("Open Existing BookText Output…"', main)

    def test_developer_controls_are_progressively_disclosed(self):
        main, settings = APP.split("struct SettingsView", 1)
        self.assertNotIn('TextField("Path to booktext.py"', main)
        self.assertNotIn('Clean Temporary Files', main)
        self.assertNotIn('Check Preflight', APP)
        self.assertNotIn('Open Primary Result', APP)
        self.assertNotIn('Final Book & Export', APP)
        self.assertIn('TextField("Path to booktext.py"', settings)
        self.assertIn('Clean Temporary Files', settings)
        self.assertIn('Advanced and Diagnostics', settings)

    def test_state_is_a_projection_and_core_remains_authoritative(self):
        for state in (
            "noDocument", "readyToProcess", "processing", "interruptedResumable",
            "reviewRequired", "readyForFinalBuild", "bookReady", "translating",
            "translationReady", "failed",
        ):
            self.assertRegex(PRESENTATION, rf"case {state}\b")
        self.assertIn('arguments: ["inspect", url.path, "--record-activity"]', APP)
        self.assertIn('arguments: ["cleanup", outputURL.path]', APP)
        self.assertNotIn("review_decisions", APP)
        self.assertNotIn("book_document.json", APP)

    def test_review_refresh_and_single_primary_action_contract(self):
        self.assertIn(".sheet(isPresented: $model.reviewPresented, onDismiss: refreshCurrentOutput)", APP)
        self.assertIn("switch model.snapshot.primaryAction", APP)
        self.assertIn('Button("Review \\(model.snapshot.unresolvedHigh + model.snapshot.unresolvedMedium) Items"', APP)
        self.assertIn('Button(model.buildingFinalBook ? "Creating Final Book…" : "Create Final Book"', APP)

    def test_export_and_translation_are_coherent_document_actions(self):
        self.assertIn("enum FinalBookSection", FINAL)
        self.assertIn("case preview, translation, export", FINAL)
        self.assertIn("ExportFormatSelection.ordered(selectedFormats)", FINAL)
        self.assertIn('Text("Formats")', FINAL)
        self.assertIn('LabeledContent("Destination")', FINAL)
        self.assertIn('"Start Translation"', FINAL)
        self.assertIn('Button("Stop", action: model.cancel)', FINAL)
        self.assertIn("operationResumed", FINAL)

    def test_resume_and_diagnostics_are_visible_without_dominating(self):
        self.assertIn("Resuming previous processing. Completed work will not be repeated.", APP)
        self.assertIn('DisclosureGroup("Details"', APP)
        self.assertIn("DocumentPresentationMapper.phaseLabel", APP)


if __name__ == "__main__":
    unittest.main()
