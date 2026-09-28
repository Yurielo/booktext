import unittest
from pathlib import Path


class NativeAppBuildWorkflowTests(unittest.TestCase):
    def test_final_bundle_is_signed_and_verified_after_contents_assembly(self):
        script = (Path(__file__).resolve().parents[1] / "scripts" / "build_booktext_app.sh").read_text(encoding="utf-8")
        copy_index = script.index('cp "$repo_root/BookTextApp/Info.plist"')
        sign_index = script.index('codesign --force --deep --sign -')
        verify_index = script.index('codesign --verify --deep --strict --verbose=4')
        self.assertLess(copy_index, sign_index)
        self.assertLess(sign_index, verify_index)
        self.assertIn('rm -rf "$app_dir"', script)
        self.assertIn('*.app', script)


if __name__ == "__main__":
    unittest.main()
