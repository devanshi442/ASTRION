import sys
import json
import os
from pathlib import Path
from src.resolver import EvidenceResolver
from src.ingestion.registry import ConnectorRegistry

def test_resolver():
    print("Testing Evidence Resolver with end-to-end live queries...")
    resolver = EvidenceResolver()

    test_cases = [
        {
            "name": "CASE A: Object/time with no matching observation (C/2020 F3 on 2020-07-12, gap expected)",
            "object": "C/2020 F3",
            "start": "2020-07-12",
            "stop": "2020-07-13",
            "step": "1d",
            "temporal_tolerance_days": 0.5
        },
        {
            "name": "CASE B: Object with nearby ZTF observations (C/2020 F3 around 2020-08-10)",
            "object": "C/2020 F3",
            "start": "2020-08-09",
            "stop": "2020-08-11",
            "step": "1d",
            "temporal_tolerance_days": 1.0
        }
    ]

    first_candidate = None

    for case in test_cases:
        print(f"\n--- Running: {case['name']} ---")
        result = resolver.resolve(
            object_designation=case["object"],
            start_time=case["start"],
            end_time=case["stop"],
            step=case["step"],
            temporal_tolerance_days=case["temporal_tolerance_days"]
        )

        print(f"Sources Checked: {result['sources_checked']}")
        print(f"Observations Found: {result['observations_found']}")
        print(f"Observations Not Found (Gaps): {result['observations_not_found']}")

        if result['coverage_gaps']:
            print("Example Gap:", result['coverage_gaps'][0]['reason'])

        if result['candidate_observations']:
            cand = result['candidate_observations'][0]
            print("\nCandidate details for CASE B:")
            print(f"- target JD: {cand['prediction']['datetime_jd']}")
            print(f"- target RA: {cand['prediction']['RA']}")
            print(f"- target DEC: {cand['prediction']['DEC']}")
            print(f"- ZTF observation timestamp: {cand['observation_timestamp_jd']}")
            print(f"- source_record_id: {cand['source_record_id']}")
            print(f"- source_uri: {cand['source_uri']}")
            print(f"- temporal separation: {cand['temporal_separation_days']:.4f} days")
            print(f"- spatial separation: {cand['spatial_separation_arcsec']:.4f} arcsec")
            print(f"- passes configured threshold: {cand['temporal_separation_days'] <= case['temporal_tolerance_days']}")

            if first_candidate is None:
                first_candidate = cand

    print("\n--- Attempting ONE FITS retrieval for CASE B ---")
    if not first_candidate:
        print("No candidate found for FITS retrieval.")
        return

    ztf = ConnectorRegistry.get_connector("irsa_ztf")
    try:
        # Request a cutout to avoid huge downloads
        r = ztf.retrieve_data(
            first_candidate['source_record_id'],
            original_uri=first_candidate['source_uri'],
            ra=first_candidate['prediction']['RA'],
            dec=first_candidate['prediction']['DEC'],
            size=0.05
        )

        if r.status_code != 200:
            print(f"FITS retrieval failed: HTTP {r.status_code}")
            print(r.text[:200])
            return

        print("HTTP success.")

        raw_dir = Path(r"E:\Downloads\ASTRION\data\raw\ztf")
        raw_dir.mkdir(parents=True, exist_ok=True)
        fits_path = raw_dir / f"{first_candidate['source_record_id']}_cutout.fits"

        with open(fits_path, "wb") as f:
            f.write(r.content)

        print(f"File exists: {fits_path.exists()}")

        from astropy.io import fits
        from astropy.wcs import WCS

        with fits.open(fits_path) as hdul:
            print("FITS opens with astropy.io.fits.")
            hdu = hdul[0]
            print(f"Primary HDU is readable: {hdu.data is not None}")

            try:
                wcs = WCS(hdu.header)
                print(f"WCS can be constructed: {wcs.has_celestial}")
            except Exception as e:
                print(f"WCS construction failed: {e}")

            print(f"Image dimensions: {hdu.data.shape if hdu.data is not None else 'No data'}")
            print(f"Basic metadata: FILTER={hdu.header.get('FILTER')}, OBSJD={hdu.header.get('OBSJD')}")

        # Write JSON provenance
        meta_dir = Path(r"E:\Downloads\ASTRION\data\metadata")
        meta_dir.mkdir(parents=True, exist_ok=True)
        meta_path = meta_dir / f"{first_candidate['source_record_id']}_prov.json"

        prov = {
            "record_id": first_candidate['source_record_id'],
            "retrieval_timestamp": "2026-10-08T22:27:00Z",
            "candidate_info": first_candidate,
            "status": "CANDIDATE (FITS retrieved, but target not confirmed)"
        }
        with open(meta_path, "w") as f:
            json.dump(prov, f, indent=2)

        print("Provenance saved successfully.")

    except Exception as e:
        print(f"Exception during retrieval/validation: {e}")

if __name__ == "__main__":
    test_resolver()
