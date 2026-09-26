import json
import unittest
from pathlib import Path

from tools.validate_integra_ocr_contract import validate_candidate_shape


FIXTURE = (
    Path(__file__).parent / "fixtures" / "integra_ocr_candidate_v0_1.json"
)


class IntegraOCRContractTests(unittest.TestCase):
    def test_fixture_has_valid_booktext_adapter_shape(self):
        validate_candidate_shape(json.loads(FIXTURE.read_text(encoding="utf-8")))

    def test_djvu_is_not_claimed_as_supported(self):
        candidate = json.loads(FIXTURE.read_text(encoding="utf-8"))
        candidate["source"]["format"] = "DJVU"
        with self.assertRaisesRegex(ValueError, "not supported"):
            validate_candidate_shape(candidate)

    def test_provider_hash_is_required(self):
        candidate = json.loads(FIXTURE.read_text(encoding="utf-8"))
        candidate["provider"]["model_sha256"] = "unknown"
        with self.assertRaisesRegex(ValueError, "model_sha256"):
            validate_candidate_shape(candidate)


if __name__ == "__main__":
    unittest.main()
