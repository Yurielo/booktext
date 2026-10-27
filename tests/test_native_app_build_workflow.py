import unittest
from pathlib import Path


class NativeAppBuildWorkflowTests(unittest.TestCase):
    def test_final_bundle_is_signed_and_verified_after_contents_assembly(self):
        script = (Path(__file__).resolve().parents[1] / "scripts" / "build_booktext_app.sh").read_text(encoding="utf-8")
        copy_index = script.index('cp "$repo_root/BookTextApp/Info.plist"')
        icon_copy_index = script.index('cp "$repo_root/BookTextApp/Resources/BookText.icns"')
        sign_index = script.index('codesign --force --deep --sign -')
        verify_index = script.index('codesign --verify --deep --strict --verbose=4')
        self.assertLess(copy_index, sign_index)
        self.assertLess(icon_copy_index, sign_index)
        self.assertLess(script.index('xattr -dr com.apple.FinderInfo'), sign_index)
        self.assertNotIn('xattr -cr', script)
        self.assertNotIn('xattr -dr com.apple.quarantine', script)
        self.assertLess(sign_index, verify_index)
        self.assertIn('rm -rf "$app_dir"', script)
        self.assertIn('*.app', script)
        self.assertIn('BookTextSourceCommit', script)
        self.assertIn('status --porcelain --untracked-files=all', script)

    def test_icon_is_declared_and_shipped_as_a_bundle_resource(self):
        root = Path(__file__).resolve().parents[1]
        info = (root / "BookTextApp" / "Info.plist").read_text(encoding="utf-8")
        icon = root / "BookTextApp" / "Resources" / "BookText.icns"
        self.assertIn("<key>CFBundleIconFile</key><string>BookText</string>", info)
        self.assertTrue(icon.is_file())
        self.assertGreater(icon.stat().st_size, 1024)


if __name__ == "__main__":
    unittest.main()
