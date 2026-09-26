import importlib.util
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


MODULE_PATH = Path(__file__).resolve().parents[1] / "tools" / "inspect_photo_set.py"
SPEC = importlib.util.spec_from_file_location("inspect_photo_set", MODULE_PATH)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class PhotoSetTests(unittest.TestCase):
    def test_records_original_and_auto_oriented_geometry(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            photo = Path(temp_dir) / "IMG_1275.JPG"
            photo.write_bytes(b"photo")
            with patch.object(MODULE, "probe_image", side_effect=[
                {"width": 4032, "height": 3024, "orientation": "RightTop"},
                {"width": 3024, "height": 4032, "orientation": "TopLeft"},
            ]):
                manifest = MODULE.inspect_photos([photo])
            item = manifest["photos"][0]
            self.assertEqual(item["transform"], "imagemagick:auto-orient")
            self.assertTrue(item["review_required"])
            self.assertEqual(item["prepared"]["height"], 4032)
