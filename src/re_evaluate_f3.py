import os
import json
import numpy as np
from pathlib import Path
from astropy.io import fits
from astropy.wcs import WCS
from astropy.coordinates import SkyCoord
import astropy.units as u
from astroquery.jplhorizons import Horizons

def re_evaluate_f3(data_dir: Path):
    raw_dir = data_dir / "raw" / "ztf"
    fits_files = list(raw_dir.glob("*_cutout.fits"))
    
    target = "C/2020 F3"
    
    results = []
    
    print(f"Found {len(fits_files)} existing C/2020 F3 cutouts. Re-evaluating...")
    
    for i, fits_path in enumerate(fits_files):
        print(f"[{i+1}/{len(fits_files)}] Processing {fits_path.name}...")
        
        try:
            with fits.open(fits_path) as hdul:
                hdu = hdul[0]
                header = hdu.header
                wcs = WCS(header)
                ny, nx = hdu.data.shape
                
                obsjd = float(header.get("OBSJD"))
                if not obsjd:
                    print("Missing OBSJD in header.")
                    continue
                
                # Query exact ephemeris
                obj_exact = Horizons(id=target, id_type="designation", location="I41", epochs=[obsjd])
                eph_table = obj_exact.ephemerides()
                
                if not eph_table:
                    print("Horizons exact query failed.")
                    continue
                    
                eph = eph_table[0]
                pred_ra = float(eph["RA"])
                pred_dec = float(eph["DEC"])
                
                # Project
                target_sky = SkyCoord(ra=pred_ra*u.deg, dec=pred_dec*u.deg, frame='icrs')
                pred_x, pred_y = wcs.world_to_pixel(target_sky)
                pred_x, pred_y = float(pred_x), float(pred_y)
                
                inside_image = (0 <= pred_x < nx) and (0 <= pred_y < ny)
                
                results.append({
                    "record_id": fits_path.name.replace("_cutout.fits", ""),
                    "observation_jd_fits": obsjd,
                    "ephemeris_jd_used": float(eph["datetime_jd"]),
                    "temporal_difference_days": abs(obsjd - float(eph["datetime_jd"])),
                    "predicted_ra": pred_ra,
                    "predicted_dec": pred_dec,
                    "predicted_x": pred_x,
                    "predicted_y": pred_y,
                    "inside_image": inside_image,
                    "status_update": "UNVERIFIED_PENDING_RELOCALIZATION",
                    "reason": "Previous point-source NO_CANDIDATE label was based on coarse 1d grid localization."
                })
                
        except Exception as e:
            print(f"Error on {fits_path.name}: {e}")
            
    out_file = data_dir / "processed" / "f3_exact_re_evaluation.json"
    with open(out_file, "w") as f:
        json.dump(results, f, indent=2)
        
    print(f"\nDone! Re-evaluated {len(results)} images. Results saved to {out_file.name}")
    
    # Just show a quick summary of how many are actually inside the existing 0.1 deg cutout
    inside_count = sum(1 for r in results if r["inside_image"])
    print(f"Number of exact predicted positions inside the existing coarse cutouts: {inside_count} / {len(results)}")

if __name__ == "__main__":
    re_evaluate_f3(Path(r"E:\Downloads\ASTRION\data"))
