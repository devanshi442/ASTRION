import os
import json
import numpy as np
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from pathlib import Path
from astropy.io import fits
from astropy.wcs import WCS
import astropy.wcs.utils
from astropy.coordinates import SkyCoord
import astropy.units as u
from astropy.table import Table, vstack
from astropy.stats import sigma_clipped_stats
from photutils.segmentation import detect_sources, SourceCatalog
from photutils.background import Background2D, MedianBackground
from astropy.convolution import Gaussian2DKernel

def analyze_moving_objects(output_dir: Path):
    manifest_path = output_dir / "dataset" / "manifests" / "harvest_manifest.jsonl"
    
    with open(manifest_path, "r") as f:
        records = [json.loads(line) for line in f]
        
    human_cases = [r for r in records if r["label_status"] == "HUMAN_VERIFICATION_REQUIRED"]
    no_cand_cases = [r for r in records if r["label_status"] == "NO_CANDIDATE"]
    
    diagnostics = {}
    catalog_list = []
    
    # Process HUMAN_VERIFICATION_REQUIRED cases
    for r in human_cases:
        obs_id = r["observation_id"]
        diagnostics[obs_id] = process_image(r, output_dir, plot=True)
        if diagnostics[obs_id]["catalog"] is not None:
            catalog_list.append(diagnostics[obs_id]["catalog"])
            
    # Process NO_CANDIDATE cases
    no_cand_stats = {"segmentation_found_new": 0, "total_segmentation_sources": 0}
    for r in no_cand_cases:
        obs_id = r["observation_id"]
        res = process_image(r, output_dir, plot=False)
        diagnostics[obs_id] = res
        if res["catalog"] is not None:
            catalog_list.append(res["catalog"])
            no_cand_stats["total_segmentation_sources"] += len(res["catalog"])
            # If we found something within 10" that DAOStarFinder missed
            if any(res["catalog"]["sep_arcsec"] < 10.0):
                no_cand_stats["segmentation_found_new"] += 1
                
    # Combine catalogs
    if len(catalog_list) > 0:
        full_catalog = vstack(catalog_list)
        full_catalog.write(output_dir / "processed" / "moving_object_candidates.ecsv", format='ascii.ecsv', overwrite=True)
        
    # Remove raw catalogs from dict before JSON dump
    for k in diagnostics:
        del diagnostics[k]["catalog"]
        
    # Save diagnostic JSON
    with open(output_dir / "processed" / "moving_object_diagnostics.json", "w") as f:
        json.dump({
            "human_verification_cases": [r["observation_id"] for r in human_cases],
            "no_candidate_cases": [r["observation_id"] for r in no_cand_cases],
            "no_candidate_segmentation_stats": no_cand_stats,
            "details": diagnostics
        }, f, indent=2)
        
    print("\n--- Diagnostic Pipeline Complete ---")
    print(f"Processed {len(human_cases)} HUMAN_VERIFICATION cases.")
    print(f"Processed {len(no_cand_cases)} NO_CANDIDATE cases.")
    print(f"Segmentation found closer sources in {no_cand_stats['segmentation_found_new']} NO_CANDIDATE cases.")

def process_image(record, output_dir, plot=False):
    fits_path = Path(record["fits_path"])
    obs_id = record["observation_id"]
    ra = record["ra_predicted"]
    dec = record["dec_predicted"]
    
    with fits.open(fits_path) as hdul:
        hdu = hdul[0]
        data = np.asarray(hdu.data, dtype=float)
        header = hdu.header
        wcs = WCS(header)
        
        target_sky = SkyCoord(ra=ra*u.deg, dec=dec*u.deg, frame='icrs')
        pred_x, pred_y = wcs.world_to_pixel(target_sky)
        pred_x, pred_y = float(pred_x), float(pred_y)
        
        pixel_scale_arcsec = record["pixel_scale"]
        
        # Local stats
        mean, median, std = sigma_clipped_stats(data, sigma=3.0)
        threshold = median + (5.0 * std)
        
        # Segmentation detection (handles extended sources better than DAOStarFinder)
        kernel = Gaussian2DKernel(x_stddev=2.0)
        kernel.normalize()
        segm = detect_sources(data, threshold, n_pixels=5)
        
        cat_table = None
        closest_source = None
        
        if segm is not None:
            cat = SourceCatalog(data, segm, wcs=wcs)
            tbl = cat.to_table()
            
            # Calculate separations
            seps = []
            for r_cat in tbl:
                sc = r_cat['sky_centroid']
                seps.append(target_sky.separation(sc).arcsec)
            tbl['sep_arcsec'] = seps
            tbl['observation_id'] = obs_id
            
            tbl.sort('sep_arcsec')
            cat_table = tbl
            
            closest = tbl[0]
            closest_source = {
                "sep_arcsec": float(closest['sep_arcsec']),
                "area": float(closest['area'].value) if hasattr(closest['area'], 'value') else float(closest['area'])
            }
            
        if plot:
            fig, ax = plt.subplots(figsize=(8,8))
            norm = matplotlib.colors.Normalize(vmin=median - std, vmax=median + 5*std)
            ax.imshow(data, cmap='viridis', origin='lower', norm=norm)
            
            ax.plot(pred_x, pred_y, 'rx', markersize=12, label="Predicted Target")
            
            if pixel_scale_arcsec:
                for rad in [5, 10, 15, 30]:
                    r_px = rad / pixel_scale_arcsec
                    c = plt.Circle((pred_x, pred_y), r_px, color='white', fill=False, linestyle='--', alpha=0.5, label=f'{rad}"')
                    ax.add_patch(c)
                    
            if segm is not None:
                ax.scatter(tbl['xcentroid'], tbl['ycentroid'], facecolors='none', edgecolors='red', s=40)
                
            ax.set_title(f"Diagnostic Case: {obs_id}\nTarget: {record['object_designation']}")
            ax.legend(loc='upper right')
            
            fig_dir = output_dir / "figures"
            fig_dir.mkdir(parents=True, exist_ok=True)
            plt.savefig(fig_dir / f"moving_object_case_{obs_id}.png", dpi=150)
            plt.close()
            
        return {
            "closest_source": closest_source,
            "catalog": cat_table,
            "background_std": float(std)
        }

if __name__ == "__main__":
    out_dir = Path(r"E:\Downloads\ASTRION\data")
    analyze_moving_objects(out_dir)
