import os
import json
import numpy as np
from pathlib import Path
from datetime import datetime, timezone
from src.resolver import EvidenceResolver
from src.ingestion.registry import ConnectorRegistry

from astropy.io import fits
from astropy.wcs import WCS
import astropy.wcs.utils
from astropy.coordinates import SkyCoord
import astropy.units as u
from astropy.stats import sigma_clipped_stats
from photutils.detection import DAOStarFinder
from photutils.background import Background2D, MedianBackground

def harvest_dataset(output_dir: Path):
    print("Starting ASTRION Observation Harvesting Pipeline")
    
    # 1. Setup Directories
    base_dir = output_dir / "dataset"
    dirs = {
        "candidates": base_dir / "candidates",
        "negatives": base_dir / "negatives",
        "uncertain": base_dir / "uncertain",
        "manifests": base_dir / "manifests"
    }
    for d in dirs.values():
        d.mkdir(parents=True, exist_ok=True)
        
    raw_dir = output_dir / "raw" / "ztf"
    raw_dir.mkdir(parents=True, exist_ok=True)
    
    resolver = EvidenceResolver()
    ztf = ConnectorRegistry.get_connector("irsa_ztf")
    
    manifest_records = []
    
    # Target list
    targets = [
        {"name": "C/2020 F3", "start": "2020-08-08", "stop": "2020-08-11", "step": "1d"},
        {"name": "1 Ceres", "start": "2020-09-01", "stop": "2020-09-03", "step": "1d"},
        {"name": "433 Eros", "start": "2020-09-01", "stop": "2020-09-03", "step": "1d"}
    ]
    
    obs_count = 0
    fits_retrieved = 0
    wcs_valid_count = 0
    inside_count = 0
    candidate_count = 0
    negative_count = 0
    human_verif_count = 0
    
    for t in targets:
        if obs_count >= 50:
            break
            
        print(f"\nProcessing {t['name']} from {t['start']} to {t['stop']}")
        result = resolver.resolve(t["name"], t["start"], t["stop"], t["step"], temporal_tolerance_days=1.0)
        
        candidates = result.get("candidate_observations", [])
        print(f"Found {len(candidates)} potential candidates for {t['name']}.")
        
        for cand in candidates:
            if obs_count >= 50:
                break
                
            obs_id = f"obs_{obs_count:04d}"
            record_id = cand["source_record_id"]
            
            # 4. Retrieve FITS (cutout to save space)
            fits_path = raw_dir / f"{record_id}_cutout.fits"
            
            if not fits_path.exists():
                try:
                    r = ztf.retrieve_data(
                        record_id,
                        original_uri=cand["source_uri"],
                        ra=cand["prediction"]["RA"],
                        dec=cand["prediction"]["DEC"],
                        size=0.05
                    )
                    if r.status_code == 200:
                        with open(fits_path, "wb") as f:
                            f.write(r.content)
                        fits_retrieved += 1
                    else:
                        print(f"Failed to retrieve {record_id} - HTTP {r.status_code}")
                        continue
                except Exception as e:
                    print(f"Error retrieving {record_id}: {e}")
                    continue
            else:
                fits_retrieved += 1
                
            # 5-9. Run Analysis
            wcs_valid = False
            inside_image = False
            pixel_scale_arcsec = None
            pred_x, pred_y = None, None
            label_status = "EPHEMERIS_COVERED"
            
            try:
                with fits.open(fits_path) as hdul:
                    hdu = hdul[0]
                    data = np.asarray(hdu.data, dtype=float)
                    header = hdu.header
                    
                    if data.ndim == 2:
                        wcs = WCS(header)
                        wcs_valid = wcs.has_celestial
                        
                        if wcs_valid:
                            wcs_valid_count += 1
                            ny, nx = data.shape
                            pred_x, pred_y = wcs.world_to_pixel_values(cand["prediction"]["RA"], cand["prediction"]["DEC"])
                            pred_x, pred_y = float(pred_x), float(pred_y)
                            
                            inside_image = (0 <= pred_x < nx) and (0 <= pred_y < ny)
                            if inside_image:
                                inside_count += 1
                                
                            pixel_scale_deg = np.mean(astropy.wcs.utils.proj_plane_pixel_scales(wcs))
                            pixel_scale_arcsec = pixel_scale_deg * 3600.0
                            
                            # Source Detection
                            if inside_image:
                                mean, median, std = sigma_clipped_stats(data, sigma=3.0)
                                threshold = 5.0 * std
                                daofind = DAOStarFinder(fwhm=3.0, threshold=threshold)
                                data_sub = data - median
                                sources = daofind(data_sub)
                                
                                nearest_sep = 999.0
                                if sources is not None and len(sources) > 0:
                                    s_coords = wcs.pixel_to_world_values(sources['xcentroid'], sources['ycentroid'])
                                    sc_sources = SkyCoord(ra=s_coords[0]*u.deg, dec=s_coords[1]*u.deg, frame='icrs')
                                    sc_target = SkyCoord(ra=cand["prediction"]["RA"]*u.deg, dec=cand["prediction"]["DEC"]*u.deg, frame='icrs')
                                    
                                    separations = sc_target.separation(sc_sources).arcsec
                                    nearest_sep = float(np.min(separations))
                                
                                if nearest_sep <= 5.0:
                                    label_status = "CANDIDATE"
                                    candidate_count += 1
                                elif nearest_sep <= 15.0:
                                    label_status = "HUMAN_VERIFICATION_REQUIRED"
                                    human_verif_count += 1
                                else:
                                    label_status = "NO_CANDIDATE"
                                    negative_count += 1
            except Exception as e:
                print(f"Error analyzing {record_id}: {e}")
                
            # Create manifest record
            rec = {
                "observation_id": obs_id,
                "object_designation": t["name"],
                "object_type": "moving_object",
                "archive": "irsa_ztf",
                "source_record_id": record_id,
                "source_uri": cand["source_uri"],
                "observation_time": cand["observation_timestamp_jd"],
                "filter": header.get("FILTER") if 'header' in locals() else None,
                "exposure_time": header.get("EXPTIME") if 'header' in locals() else None,
                "ra_predicted": cand["prediction"]["RA"],
                "dec_predicted": cand["prediction"]["DEC"],
                "x_predicted": pred_x,
                "y_predicted": pred_y,
                "pixel_scale": pixel_scale_arcsec,
                "wcs_valid": wcs_valid,
                "inside_image": inside_image,
                "temporal_separation_days": cand["temporal_separation_days"],
                "candidate_status": cand["resolution_status"],
                "label_status": label_status,
                "fits_path": str(fits_path.resolve()),
                "pipeline_version": "ASTRION-Harvest-0.1"
            }
            manifest_records.append(rec)
            
            # Write a pointer file in the respective folder
            folder_map = {
                "CANDIDATE": "candidates",
                "NO_CANDIDATE": "negatives",
                "HUMAN_VERIFICATION_REQUIRED": "uncertain",
                "EPHEMERIS_COVERED": "uncertain"
            }
            pointer_path = dirs[folder_map[label_status]] / f"{obs_id}.json"
            with open(pointer_path, "w") as f:
                json.dump({"fits_path": rec["fits_path"], "label": label_status}, f)
                
            obs_count += 1
            if obs_count % 5 == 0:
                print(f"Processed {obs_count} observations...")

    # Save manifest
    manifest_path = dirs["manifests"] / "harvest_manifest.jsonl"
    with open(manifest_path, "w") as f:
        for r in manifest_records:
            f.write(json.dumps(r) + "\n")
            
    print("\n--- Harvesting Summary ---")
    print(f"Total observations found and processed: {obs_count}")
    print(f"FITS files successfully retrieved: {fits_retrieved}")
    print(f"Images with valid WCS: {wcs_valid_count}")
    print(f"Targets inside predicted footprint: {inside_count}")
    print(f"Candidate detections (<=5\"): {candidate_count}")
    print(f"Non-detections (NO_CANDIDATE): {negative_count}")
    print(f"Require human verification (5-15\"): {human_verif_count}")
    
    if candidate_count == 0 and negative_count > 0:
        print("\nNote: ZTF cutouts alone produced no definitive candidates for these dates.")
        print("This suggests the need for deeper optical archives (MAST) or wider temporal windows.")

if __name__ == "__main__":
    out_dir = Path(r"E:\Downloads\ASTRION\data")
    harvest_dataset(out_dir)
