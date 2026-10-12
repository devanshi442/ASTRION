import sys
import time
from pathlib import Path
sys.path.append(r"E:\Downloads\ASTRION")

from src.dataset_generator import generate_dataset
import requests

# Monkey patch requests to count calls
original_get = requests.get
request_count = 0

def counting_get(*args, **kwargs):
    global request_count
    request_count += 1
    return original_get(*args, **kwargs)

requests.get = counting_get

def run_pilot():
    global request_count
    print("--- Running ASTRION Pilot Acquisition ---")

    # 1. Target only Bennu
    pilot_targets = [
        {"name": "101955 Bennu", "id": "101955", "id_type": "smallbody", "split": "val"},
    ]

    # 2. Use isolated test directory
    test_dir = Path(r"E:\Downloads\ASTRION\data\test_pilot")
    test_dir.mkdir(parents=True, exist_ok=True)

    # Clean previous test manifest if it exists
    man_path = test_dir / "manifest.json"
    if man_path.exists():
        man_path.unlink()

    # 3. Use a 1-month window
    test_epochs = {"start": "2019-06-15", "stop": "2019-07-15", "step": "1d"}

    start_time = time.time()

    # 4. Strict cap of 3 samples
    generate_dataset(
        targets=pilot_targets,
        dataset_dir=test_dir,
        images_per_object=3,
        epochs=test_epochs
    )

    elapsed = time.time() - start_time

    # Results
    print("\n--- Pilot Results ---")
    print(f"Elapsed Time: {elapsed:.2f} seconds")
    print(f"Total Network Requests (requests.get): {request_count}")

    if man_path.exists():
        import json
        with open(man_path, "r") as f:
            manifest = json.load(f)
        print(f"Valid Cutouts Produced: {len(manifest)}")
        for i, m in enumerate(manifest):
            print(f"  [{i+1}] {m['record_id']} - {m['label']} - {m['obsjd']}")
    else:
        print("Valid Cutouts Produced: 0")

if __name__ == "__main__":
    run_pilot()
