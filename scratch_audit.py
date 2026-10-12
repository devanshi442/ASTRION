import json
import os
import glob

def check_manifests():
    dir_path = "data/dataset_v1"

    m_path = os.path.join(dir_path, "manifest.json")
    mb_path = os.path.join(dir_path, "manifest.json.bak")
    mt_path = os.path.join(dir_path, "manifest.json.test_bak")

    def load_json(p):
        if os.path.exists(p):
            with open(p, 'r') as f:
                return json.load(f)
        return []

    m = load_json(m_path)
    mb = load_json(mb_path)
    mt = load_json(mt_path)

    print(f"manifest.json entries: {len(m)}")
    print(f"manifest.json.bak entries: {len(mb)}")
    print(f"manifest.json.test_bak entries: {len(mt)}")

    r_m = {x['record_id'] for x in m}
    r_mb = {x['record_id'] for x in mb}
    r_mt = {x['record_id'] for x in mt}

    added = r_m - r_mb
    removed = r_mb - r_m

    print(f"Added to manifest: {added}")
    print(f"Removed from manifest: {removed}")

    labels = [x['label'] for x in m]
    pos = labels.count('CONFIRMED_POSITIVE')
    neg = labels.count('HARD_NEGATIVE')
    print(f"Labels in manifest: POS={pos}, NEG={neg}")

    fits = glob.glob(f"{dir_path}/*.fits")
    pngs = glob.glob(f"{dir_path}/*.png")

    print(f"FITS files: {len(fits)}")
    print(f"PNG files: {len(pngs)}")

    # Check for the two new cutouts in manifest
    # ztf_20190615173287_000470_c08_q2
    # ztf_20190628175093_000420_c15_q4
    pilot_c1 = any("ztf_20190615173287_000470_c08_q2" in x['record_id'] for x in m)
    pilot_c2 = any("ztf_20190628175093_000420_c15_q4" in x['record_id'] for x in m)
    print(f"Pilot cutout 1 in production manifest: {pilot_c1}")
    print(f"Pilot cutout 2 in production manifest: {pilot_c2}")

check_manifests()
