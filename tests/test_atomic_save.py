import sys
import unittest
import os
import json
import tempfile
from pathlib import Path
REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
from src.dataset_generator import atomic_save_manifest

class TestAtomicSave(unittest.TestCase):
    def setUp(self):
        self.test_dir = tempfile.TemporaryDirectory()
        self.man_path = Path(self.test_dir.name) / "manifest.json"

        # Write initial manifest
        self.initial_data = [{"object": "101955 Bennu", "record_id": "rec_1"}]
        with open(self.man_path, "w") as f:
            json.dump(self.initial_data, f)

    def tearDown(self):
        self.test_dir.cleanup()

    def test_successful_save(self):
        new_data = [{"object": "101955 Bennu", "record_id": "rec_1"}, {"object": "101955 Bennu", "record_id": "rec_2"}]
        atomic_save_manifest(new_data, self.man_path)

        with open(self.man_path, "r") as f:
            saved_data = json.load(f)

        self.assertEqual(len(saved_data), 2)

    def test_failed_save_preserves_original(self):
        # We will mock json.dump to raise an exception, simulating a crash during save
        import unittest.mock
        new_data = [{"object": "101955 Bennu", "record_id": "rec_1"}, {"object": "101955 Bennu", "record_id": "rec_2"}]

        with unittest.mock.patch('src.dataset_generator.json.dump', side_effect=Exception("Simulated Crash")):
            with self.assertRaises(Exception):
                atomic_save_manifest(new_data, self.man_path)

        # The original manifest should be untouched
        with open(self.man_path, "r") as f:
            saved_data = json.load(f)

        self.assertEqual(len(saved_data), 1)
        self.assertEqual(saved_data[0]["record_id"], "rec_1")

if __name__ == '__main__':
    unittest.main()
