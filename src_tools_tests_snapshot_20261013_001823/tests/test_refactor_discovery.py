import sys
import unittest
import json
import tempfile
from unittest.mock import patch, MagicMock
from pathlib import Path
REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
from src.dataset_generator import generate_dataset
from src.ingestion.ztf import ZTFConnector
from astropy.io import fits
import requests

class TestRefactorDiscovery(unittest.TestCase):
    def setUp(self):
        self.test_dir = tempfile.TemporaryDirectory()
        self.dataset_dir = Path(self.test_dir.name)
        self.man_path = self.dataset_dir / "manifest.json"

        self.mock_horizons = patch('src.dataset_generator.Horizons').start()
        self.mock_obj_coarse = MagicMock()
        self.mock_obj_coarse.ephemerides.return_value = [{"RA": "150.0", "DEC": "20.0", "datetime_jd": "2458649.0"}]
        self.mock_horizons.return_value = self.mock_obj_coarse

        self.mock_get_connector = patch('src.dataset_generator.ConnectorRegistry.get_connector').start()
        self.mock_ztf = MagicMock()
        self.mock_get_connector.return_value = self.mock_ztf

        self.mock_cutout = patch('src.dataset_generator.Cutout2D').start()
        self.mock_fits = patch('src.dataset_generator.fits').start()
        self.mock_plt = patch('src.dataset_generator.plt').start()

        self.test_targets = [{"name": "1 Ceres", "id": "1", "id_type": "smallbody", "split": "train"}]

    def tearDown(self):
        patch.stopall()
        self.test_dir.cleanup()

    def test_quota_skipping(self):
        # 1 Ceres is requested, quota is 1. Give it 1 in manifest.
        manifest_data = [{"object": "1 Ceres", "record_id": "rec_1", "split": "train", "label": "CONFIRMED_POSITIVE", "filter": "ZTF_r", "snr": 10.0, "angular_separation": 1.0, "obsjd": 2458649.0}]
        with open(self.man_path, "w") as f:
            json.dump(manifest_data, f)

        generate_dataset(targets=self.test_targets, dataset_dir=self.dataset_dir, images_per_object=1)
        # Horizons should NOT be called because quota is fulfilled
        self.mock_horizons.assert_not_called()
        self.mock_ztf.query_metadata.assert_not_called()

    def test_caching_metadata(self):
        # No manifest, so quota is 0/1. Need 1.
        self.mock_ztf.query_metadata.return_value = [{"record_id": "rec_new", "obsjd": "2458649.0"}]

        # We will write to the meta_cache.json
        meta_cache = {"1 Ceres_2458649.0": [{"record_id": "rec_cached", "obsjd": "2458649.0"}]}
        with open(self.dataset_dir / "meta_cache.json", "w") as f:
            json.dump(meta_cache, f)

        generate_dataset(targets=self.test_targets, dataset_dir=self.dataset_dir, images_per_object=1)

        # Because it's cached, query_metadata should only be called for the EXACT match, not coarse!
        # Wait, exact cache key is '1 Ceres_exact_2458649.00000'. It's not in cache, so exact will be called.
        self.assertEqual(self.mock_ztf.query_metadata.call_count, 1) # Only exact

    @patch('src.ingestion.ztf.Table.read')
    @patch('src.ingestion.ztf.requests.get')
    def test_ztf_retry_backoff(self, mock_requests_get, mock_table_read):
        connector = ZTFConnector()
        mock_resp = MagicMock()
        mock_resp.text = "line1\nline2\nline3\nline4" # Enough lines to pass the len check
        mock_requests_get.side_effect = [requests.exceptions.ConnectionError("Fail 1"), requests.exceptions.Timeout("Fail 2"), mock_resp]

        class MockRow(dict):
            @property
            def colnames(self):
                return ["obsjd", "filefracday", "field", "ccdid", "qid", "filtercode", "imgtypecode"]

        # Mock table read to return a mock table that mimics astropy Table
        mock_tbl = MagicMock()
        mock_tbl.colnames = ["obsjd", "filefracday", "field", "ccdid", "qid", "filtercode", "imgtypecode"]
        mock_row = MockRow({"obsjd": 2458649.0, "filefracday": 20190101, "field": 1, "ccdid": 1, "qid": 1, "filtercode": "r", "imgtypecode": "sci"})
        mock_tbl.__iter__.return_value = iter([mock_row])
        mock_table_read.return_value = mock_tbl

        with patch('time.sleep') as mock_sleep:
            res = connector.query_metadata(ra=150.0, dec=20.0, max_retries=3)
            self.assertEqual(len(res), 1)
            self.assertEqual(mock_requests_get.call_count, 3)
            # Should have slept twice
            self.assertEqual(mock_sleep.call_count, 2)
            calls = [call[0][0] for call in mock_sleep.call_args_list]
            self.assertTrue(1.0 <= calls[0] <= 2.0)
            self.assertTrue(2.0 <= calls[1] <= 3.0)

    def test_comet_labeling(self):
        targets = [{"name": "C/2020 F3", "id": "C/2020 F3", "id_type": "designation", "split": "test"}]

        self.mock_ztf.query_metadata.side_effect = [
            [{"record_id": "rec_comet", "obsjd": "2458649.0"}],
            [{"record_id": "rec_comet", "obsjd": "2458649.0", "original_uri": "test"}]
        ]
        self.mock_ztf.retrieve_data.return_value = "dummy.fits"

        with patch('src.dataset_generator.atomic_save_manifest') as mock_save:
            with patch('src.dataset_generator.detect_sources', return_value=None):
                with patch('src.dataset_generator.Cutout2D') as mock_cutout2d:
                    import numpy as np
                    mock_cutout_inst = MagicMock()
                    mock_cutout_inst.data = np.zeros((10, 10))
                    mock_cutout_inst.position_cutout = (0, 0)
                    mock_cutout_inst.origin_original = (0, 0)
                    mock_cutout_inst.wcs.to_header.return_value = fits.Header()
                    mock_cutout2d.return_value = mock_cutout_inst
                    mock_hdu = MagicMock()
                    mock_hdu.data = np.zeros((10, 10))
                    mock_hdu.header = fits.Header()
                    mock_hdul = MagicMock()
                    mock_hdul.__enter__.return_value = [mock_hdu]
                    self.mock_fits.open.return_value = mock_hdul

                    with patch('src.dataset_generator.WCS') as mock_wcs_class:
                        mock_wcs_inst = MagicMock()
                        mock_wcs_inst.world_to_pixel.return_value = (0, 0)
                        mock_wcs_inst.world_to_pixel_values.return_value = (0, 0)
                        mock_wcs_class.return_value = mock_wcs_inst

                        generate_dataset(targets=targets, dataset_dir=self.dataset_dir, images_per_object=1)

            self.assertEqual(mock_save.call_count, 1)
            saved_manifest = mock_save.call_args[0][0]

            self.assertEqual(len(saved_manifest), 1)
            self.assertEqual(saved_manifest[0]["label"], "UNVERIFIED_PENDING_RELOCALIZATION")
            self.assertEqual(saved_manifest[0]["negative_reason"], "COMET_DIFFUSE_MORPHOLOGY")

if __name__ == '__main__':
    unittest.main()
