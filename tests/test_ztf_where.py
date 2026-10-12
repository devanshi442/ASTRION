import sys
import os
import json
import pytest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from src.ingestion.registry import ConnectorRegistry

# Mark entire module as live_network and skip unless RUN_LIVE_TESTS is set
pytestmark = [
    pytest.mark.live_network,
    pytest.mark.skipif(
        not os.getenv("RUN_LIVE_TESTS"),
        reason="Live network test requires explicit opt-in (set RUN_LIVE_TESTS=1)"
    )
]

def test_temporal_filtering():
    print("--- 1. Testing ZTF Temporal Filtering ---")
    ztf = ConnectorRegistry.get_connector("irsa_ztf")

    ra, dec = 150.0, 20.0
    test_jd = 2458649.0

    res_filtered = ztf.query_metadata(ra=ra, dec=dec, start_jd=test_jd, end_jd=test_jd + 1.0)

    # Assert obsjd values are inside requested bounds
    for obs in res_filtered:
        obsjd = float(obs["obsjd"])
        assert test_jd < obsjd < test_jd + 1.0, f"obsjd {obsjd} outside bounds!"

    print(f"[PASS] Filtering works. Returned {len(res_filtered)} records, all within bounds.")

def test_bennu_localization():
    print("\n--- 2. Testing Fast-Mover Localization (Bennu) ---")
    from astroquery.jplhorizons import Horizons
    ztf = ConnectorRegistry.get_connector("irsa_ztf")

    # Bennu, step 1d
    obj_coarse = Horizons(id="101955", id_type="smallbody", location="I41",
                          epochs={"start": "2019-06-16", "stop": "2019-06-25", "step": "1d"})

    eph_coarse = obj_coarse.ephemerides()

    matches_found = 0
    for row in eph_coarse:
        coarse_ra = float(row["RA"])
        coarse_dec = float(row["DEC"])
        coarse_jd = float(row["datetime_jd"])

        ztf_obs_coarse = ztf.query_metadata(ra=coarse_ra, dec=coarse_dec,
                                            start_jd=coarse_jd - 1.0,
                                            end_jd=coarse_jd + 1.0)
        if not ztf_obs_coarse:
            continue

        distinct_obsjds = list(set(float(obs["obsjd"]) for obs in ztf_obs_coarse))

        for obsjd in distinct_obsjds:
            obj_exact = Horizons(id="101955", id_type="smallbody", location="I41", epochs=[obsjd])
            eph_exact = obj_exact.ephemerides()[0]

            pred_ra = float(eph_exact["RA"])
            pred_dec = float(eph_exact["DEC"])

            ztf_obs_exact = ztf.query_metadata(ra=pred_ra, dec=pred_dec,
                                               start_jd=obsjd - 0.0001, end_jd=obsjd + 0.0001)

            if ztf_obs_exact:
                record_id = ztf_obs_exact[0]["record_id"]
                print(f"[SUCCESS] Exact quadrant found for Bennu at JD {obsjd}: {record_id}")
                matches_found += 1
                break

        if matches_found >= 2:
            break

    assert matches_found > 0, "No new samples localized in the test window."

if __name__ == "__main__":
    if os.getenv("RUN_LIVE_TESTS"):
        test_temporal_filtering()
        test_bennu_localization()
    else:
        print("Skipping live network test (set RUN_LIVE_TESTS=1 to run).")
