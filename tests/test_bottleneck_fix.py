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

class TestBottleneckFix(unittest.TestCase):
    def setUp(self):
        self.test_dir = tempfile.TemporaryDirectory()
        self.dataset_dir = Path(self.test_dir.name)
        self.man_path = self.dataset_dir / "manifest.json"

        self.mock_ztf = MagicMock()
        self.mock_horizons = patch('src.dataset_generator.Horizons').start()
        self.mock_get_connector = patch('src.dataset_generator.ConnectorRegistry.get_connector').start()
        self.mock_get_connector.return_value = self.mock_ztf

        self.mock_cutout = patch('src.dataset_generator.Cutout2D').start()
        self.mock_fits = patch('src.dataset_generator.fits').start()
        self.mock_save = patch('src.dataset_generator.atomic_save_manifest').start()
        self.mock_plt = patch('src.dataset_generator.plt').start()

        self.mock_obj_coarse = MagicMock()
        self.mock_obj_coarse.ephemerides.return_value = [{"RA": "150.0", "DEC": "20.0", "datetime_jd": "2458649.0"}]

        self.mock_obj_exact = MagicMock()
        self.mock_obj_exact.ephemerides.return_value = [{"RA": "150.1", "DEC": "20.1", "datetime_jd": "2458649.5"}]

        def horizons_side_effect(**kwargs):
            if 'epochs' in kwargs and isinstance(kwargs['epochs'], list) and len(kwargs['epochs']) == 1:
                return self.mock_obj_exact
            return self.mock_obj_coarse

        self.mock_horizons.side_effect = horizons_side_effect

        self.test_targets = [{"name": "101955 Bennu", "id": "101955", "id_type": "smallbody", "split": "val"}]

    def tearDown(self):
        patch.stopall()
        self.test_dir.cleanup()

    def test_already_seen_record(self):
        """If all coarse records are already seen, no exact Horizons query should be made."""
        self.mock_ztf.query_metadata.return_value = [{"record_id": "rec_1", "obsjd": "2458649.0"}]
        manifest_data = [{"object": "101955 Bennu", "record_id": "rec_1", "split": "val",
                          "label": "CONFIRMED_POSITIVE", "filter": "ZTF_r", "snr": 10.0,
                          "angular_separation": 1.0, "obsjd": 2458649.0}]
        with open(self.man_path, "w") as f:
            json.dump(manifest_data, f)

        generate_dataset(targets=self.test_targets, dataset_dir=self.dataset_dir, images_per_object=30)

        # Coarse Horizons should be called (for the ephemeris), but
        # the coarse ZTF metadata query returns rec_1 which is already seen,
        # so no exact Horizons query should happen.
        self.assertEqual(self.mock_horizons.call_count, 1)  # Coarse only

    def test_same_record_different_object(self):
        """A record seen for Apophis should NOT block the same record_id for Bennu."""
        self.mock_ztf.query_metadata.side_effect = [
            [{"record_id": "rec_1", "obsjd": "2458649.0"}],  # Coarse
            [{"record_id": "rec_1", "obsjd": "2458649.0", "original_uri": "test"}]  # Exact
        ]
        manifest_data = [{"object": "99942 Apophis", "record_id": "rec_1", "split": "train",
                          "label": "CONFIRMED_POSITIVE", "filter": "ZTF_r", "snr": 10.0,
                          "angular_separation": 1.0, "obsjd": 2458649.0}]
        with open(self.man_path, "w") as f:
            json.dump(manifest_data, f)

        generate_dataset(targets=self.test_targets, dataset_dir=self.dataset_dir, images_per_object=30)

        # Horizons: 1 coarse + 1 exact (rec_1 is NOT seen for Bennu)
        self.assertEqual(self.mock_horizons.call_count, 2)

    def test_mixture_seen_and_unseen(self):
        """With 2 records in coarse results, one seen and one unseen, only the unseen triggers exact query."""
        self.mock_ztf.query_metadata.side_effect = [
            [{"record_id": "rec_1", "obsjd": "2458649.0"}, {"record_id": "rec_2", "obsjd": "2458649.5"}],  # Coarse
            [{"record_id": "rec_2", "obsjd": "2458649.5", "original_uri": "test"}]  # Exact for rec_2
        ]
        manifest_data = [{"object": "101955 Bennu", "record_id": "rec_1", "split": "val",
                          "label": "CONFIRMED_POSITIVE", "filter": "ZTF_r", "snr": 10.0,
                          "angular_separation": 1.0, "obsjd": 2458649.0}]
        with open(self.man_path, "w") as f:
            json.dump(manifest_data, f)

        generate_dataset(targets=self.test_targets, dataset_dir=self.dataset_dir, images_per_object=30)

        # Coarse Horizons + at least 1 exact (for the unseen obsjd 2458649.5)
        # rec_1 is seen, but distinct_obsjds contains both 2458649.0 and 2458649.5
        # The exact Horizons loop iterates over BOTH obsjds (it checks record_id after exact query)
        self.assertGreaterEqual(self.mock_horizons.call_count, 2)

    def test_empty_coarse_response(self):
        """When ZTF returns no observations, only the coarse Horizons is called."""
        self.mock_ztf.query_metadata.return_value = []
        generate_dataset(targets=self.test_targets, dataset_dir=self.dataset_dir, images_per_object=30)
        self.assertEqual(self.mock_horizons.call_count, 1)  # Coarse only

    def test_multiple_unseen_records(self):
        """With multiple unseen records, exact Horizons should be called and break after first match."""
        self.mock_ztf.query_metadata.side_effect = [
            [{"record_id": "rec_1", "obsjd": "2458649.0"}, {"record_id": "rec_2", "obsjd": "2458649.0"}],
            [{"record_id": "rec_1", "obsjd": "2458649.0", "original_uri": "test"}]
        ]
        generate_dataset(targets=self.test_targets, dataset_dir=self.dataset_dir, images_per_object=30)
        # Coarse + 1 Exact (only 1 distinct obsjd, breaks after match_found)
        self.assertEqual(self.mock_horizons.call_count, 2)

    def test_exact_time_localization_not_bypassed(self):
        """Even for new records, exact Horizons must be called to verify the position."""
        self.mock_ztf.query_metadata.side_effect = [
            [{"record_id": "rec_1", "obsjd": "2458649.0"}],
            [{"record_id": "rec_1", "obsjd": "2458649.0", "original_uri": "test"}]
        ]
        generate_dataset(targets=self.test_targets, dataset_dir=self.dataset_dir, images_per_object=30)
        # Horizons must be called exactly 2 times (1 coarse, 1 exact)
        self.assertEqual(self.mock_horizons.call_count, 2)
        # Ensure the 2nd call had epochs=[2458649.0]
        self.mock_horizons.assert_called_with(id='101955', id_type='smallbody', location='I41', epochs=[2458649.0])

if __name__ == '__main__':
    unittest.main()
