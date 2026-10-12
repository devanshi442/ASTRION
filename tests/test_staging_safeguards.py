import sys
import unittest
import json
import tempfile
from pathlib import Path
from unittest.mock import patch, MagicMock

sys.path.append(r"E:\Downloads\ASTRION")
from src.dataset_generator import generate_dataset
from src.ingestion.ztf import ZTFConnector

class TestStagingSafeguards(unittest.TestCase):
    def setUp(self):
        self.test_dir = tempfile.TemporaryDirectory()
        self.dataset_dir = Path(self.test_dir.name)
        self.man_path = self.dataset_dir / "manifest.json"

    def tearDown(self):
        self.test_dir.cleanup()

    def test_audit_file_written_to_staging(self):
        """When dataset_dir is not dataset_v1, AUDIT.md must be written inside staging dataset_dir."""
        mock_horizons = patch('src.dataset_generator.Horizons').start()
        mock_obj = MagicMock()
        mock_obj.ephemerides.return_value = []
        mock_horizons.return_value = mock_obj

        # Run with empty manifest
        generate_dataset(targets=[], dataset_dir=self.dataset_dir, max_new_samples=1)
        patch.stopall()

        staging_audit = self.dataset_dir / "AUDIT.md"
        self.assertTrue(staging_audit.exists(), "AUDIT.md was not written in staging directory")

    def test_ztf_connector_default_timeout_30s(self):
        """ZTF connector default timeout must be 30 seconds."""
        connector = ZTFConnector()
        with patch('requests.get') as mock_get:
            mock_resp = MagicMock()
            mock_resp.text = "line1\nline2\nline3\nline4"
            mock_get.return_value = mock_resp
            with patch('astropy.table.Table.read') as mock_read:
                mock_read.return_value = []
                connector.query_metadata(ra=120.0, dec=20.0)
                mock_get.assert_called_once()
                self.assertEqual(mock_get.call_args[1].get("timeout"), 30)

if __name__ == '__main__':
    unittest.main()
