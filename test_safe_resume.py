import sys
import shutil
import json
import time
from pathlib import Path
sys.path.append(r"E:\Downloads\ASTRION")

from src.dataset_generator import generate_dataset
import requests

original_get = requests.get
request_count = 0

def counting_get(*args, **kwargs):
    global request_count
    request_count += 1
    return original_get(*args, **kwargs)

requests.get = counting_get

def test_safe_resume():
    prod_manifest = Path(r"E:\Downloads\ASTRION\data\dataset_v1\manifest.json")
    test_dir = Path(r"E:\Downloads\ASTRION\data\test_resume")
    test_dir.mkdir(parents=True, exist_ok=True)
    test_manifest = test_dir / "manifest.json"

    # 1. Copy production manifest to temporary test directory
    shutil.copy(prod_manifest, test_manifest)

    with open(test_manifest, "r") as f:
        initial_data = json.load(f)
    initial_count = len(initial_data)

    # 2. Run generator for Apophis using resume dir, target window 1 month
    # We will set a quota that is HIGHER than the current number of Apophis samples
    # Apophis currently has 30 samples. We'll set the quota to 32 (so it attempts to find 2 more)

    # Let's count existing Apophis samples
    apophis_count = len([x for x in initial_data if x["object"] == "99942 Apophis"])

    targets = [
        {"name": "99942 Apophis", "id": "99942", "id_type": "smallbody", "split": "train"},
    ]

    # 30 day window where we know it will search
    epochs = {"start": "2019-01-01", "stop": "2019-02-01", "step": "1d"}

    print(f"--- Safe Resume Test ---")
    print(f"Initial Manifest Size: {initial_count}")
    print(f"Initial Apophis Samples: {apophis_count}")
    print(f"Targeting quota of: {apophis_count + 2}")

    generate_dataset(
        targets=targets,
        dataset_dir=test_dir,
        images_per_object=apophis_count + 2,
        epochs=epochs
    )

    with open(test_manifest, "r") as f:
        final_data = json.load(f)

    final_count = len(final_data)
    final_apophis = len([x for x in final_data if x["object"] == "99942 Apophis"])

    print("\n--- Resume Test Results ---")
    print(f"Final Manifest Size: {final_count} (Delta: {final_count - initial_count})")
    print(f"Final Apophis Samples: {final_apophis} (Delta: {final_apophis - apophis_count})")
    print(f"Total API Requests: {request_count}")

    # 3. Verify existing untouched
    initial_ids = {x['record_id'] for x in initial_data}
    final_ids = {x['record_id'] for x in final_data}
    missing = initial_ids - final_ids
    if missing:
        print(f"ERROR: Existing records were deleted! {missing}")
    else:
        print("SUCCESS: All initial records were preserved.")

if __name__ == "__main__":
    test_safe_resume()
