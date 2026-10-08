import os
import json
import numpy as np
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from pathlib import Path
from astropy.io import fits
from astropy.wcs import WCS
import astropy
import astropy.wcs.utils
from astropy.coordinates import SkyCoord
import astropy.units as u
from astropy.stats import sigma_clipped_stats
from photutils.detection import DAOStarFinder
from astropy.nddata import Cutout2D

def diagnostic_analysis(fits_path: Path, prov_path: Path, output_dir: Path):
    print(f"Starting Diagnostic Analysis for {fits_path.name}")
    
    with open(prov_path, "r") as f:
        prov = json.load(f)
    
    cand_info = prov.get("candidate_info", {})
    pred = cand_info.get("prediction", {})
    target_ra = pred.get("RA")
    target_dec = pred.get("DEC")
    record_id = prov.get("record_id")
    
    with fits.open(fits_path) as hdul:
        hdu = hdul[0]
        data = np.asarray(hdu.data, dtype=float)
        header = hdu.header
        
        # 1. Header Inspection
        obsjd = header.get('OBSJD')
        filter_name = header.get('FILTER')
        exptime = header.get('EXPTIME')
        telescope = header.get('TELESCOP')
        instrument = header.get('INSTRUME')
        bunit = header.get('BUNIT')
        wcs = WCS(header)
        
        header_info = {
            "OBSJD": obsjd,
            "FILTER": filter_name,
            "EXPTIME": exptime,
            "TELESCOP": telescope,
            "INSTRUME": instrument,
            "BUNIT": bunit,
        }
        
        # 11. Precise WCS footprint check
        ny, nx = data.shape
        corners_px = [(0,0), (nx-1,0), (nx-1,ny-1), (0,ny-1)]
        corners_world = wcs.pixel_to_world_values(*zip(*corners_px))
        
        target_sky = SkyCoord(ra=target_ra*u.deg, dec=target_dec*u.deg, frame='icrs')
        target_x, target_y = wcs.world_to_pixel(target_sky)
        target_x, target_y = float(target_x), float(target_y)
        
        is_inside = (0 <= target_x < nx) and (0 <= target_y < ny)
        
        # 2, 7, 8. Inspect pixel region and local background
        local_size = 40
        try:
            local_cutout = Cutout2D(data, (target_x, target_y), (local_size, local_size), wcs=wcs)
            local_data = local_cutout.data
            
            mean, median, std = sigma_clipped_stats(local_data, sigma=3.0)
            
            nans_count = np.isnan(local_data).sum()
            max_val = np.nanmax(local_data) if nans_count < local_data.size else np.nan
            min_val = np.nanmin(local_data) if nans_count < local_data.size else np.nan
            
            # Simple heuristic for ZTF saturation
            is_saturated = max_val > 55000 if not np.isnan(max_val) else False
            
            # Edge effects (if cutout is padded or truncated)
            edge_effect = local_data.shape != (local_size, local_size)
        except Exception as e:
            local_data = None
            mean, median, std = np.nan, np.nan, np.nan
            nans_count, max_val, min_val, is_saturated, edge_effect = 0, np.nan, np.nan, False, True

        local_stats = {
            "mean": float(mean),
            "median": float(median),
            "std": float(std),
            "nans": int(nans_count),
            "max_val": float(max_val),
            "min_val": float(min_val),
            "is_saturated": bool(is_saturated),
            "edge_effect": bool(edge_effect)
        }
        
        # 5, 6, 9. Source Detection at multiple thresholds
        pixel_scale_deg = np.mean(astropy.wcs.utils.proj_plane_pixel_scales(wcs))
        pixel_scale_arcsec = pixel_scale_deg * 3600.0
        
        # Global stats for global thresholding
        g_mean, g_median, g_std = sigma_clipped_stats(data, sigma=3.0)
        data_sub = data - g_median
        
        sigmas = [3.0, 5.0, 7.0]
        radii_arcsec = [5.0, 10.0, 20.0, 30.0]
        detection_results = {}
        
        best_candidate = None
        
        for sig in sigmas:
            threshold = sig * g_std
            daofind = DAOStarFinder(fwhm=3.0, threshold=threshold)
            sources = daofind(data_sub)
            
            det_radii = {r: 0 for r in radii_arcsec}
            nearest_sep = None
            elongated_traits = {}
            
            if sources is not None and len(sources) > 0:
                source_coords = wcs.pixel_to_world_values(sources['xcentroid'], sources['ycentroid'])
                source_ras, source_decs = source_coords[0], source_coords[1]
                sc_sources = SkyCoord(ra=source_ras*u.deg, dec=source_decs*u.deg, frame='icrs')
                separations = target_sky.separation(sc_sources).arcsec
                
                for r in radii_arcsec:
                    det_radii[r] = int(np.sum(separations <= r))
                    
                nearest_idx = np.argmin(separations)
                nearest_sep = float(separations[nearest_idx])
                
                # Check sharpness/roundness for the nearest source if it's within 30"
                if nearest_sep <= 30.0:
                    elongated_traits = {
                        "roundness1": float(sources['roundness1'][nearest_idx]),
                        "roundness2": float(sources['roundness2'][nearest_idx]),
                        "sharpness": float(sources['sharpness'][nearest_idx])
                    }
                    if sig == 3.0:
                        best_candidate = {
                            "separation": nearest_sep,
                            "roundness": float(sources['roundness1'][nearest_idx])
                        }
            
            detection_results[f"{int(sig)}_sigma"] = {
                "threshold": float(threshold),
                "total_sources": len(sources) if sources is not None else 0,
                "counts_within_arcsec": det_radii,
                "nearest_separation_arcsec": nearest_sep,
                "nearest_shape": elongated_traits
            }
            
        # 13. Classification logic
        classification = "NO_CANDIDATE"
        if not is_inside:
            classification = "NOT_IN_IMAGE"
        elif best_candidate and best_candidate["separation"] <= 5.0:
            classification = "CANDIDATE"
        elif best_candidate and best_candidate["separation"] <= 15.0:
            classification = "AMBIGUOUS_CANDIDATE"
            
        # 3, 4. Plotting Cutouts
        fig, axs = plt.subplots(1, 4, figsize=(16, 4))
        
        # Plot 1: 80x80
        cut80 = Cutout2D(data, (target_x, target_y), (80, 80), wcs=wcs)
        norm80 = matplotlib.colors.Normalize(vmin=g_median - g_std, vmax=g_median + 5*g_std)
        axs[0].imshow(cut80.data, cmap='viridis', origin='lower', norm=norm80)
        axs[0].plot(40, 40, 'rx', markersize=10) # Center
        axs[0].set_title('80x80 Cutout')
        
        # Plot 2: 40x40
        cut40 = Cutout2D(data, (target_x, target_y), (40, 40), wcs=wcs)
        axs[1].imshow(cut40.data, cmap='viridis', origin='lower', norm=norm80)
        axs[1].plot(20, 20, 'rx', markersize=10)
        axs[1].set_title('40x40 Cutout')
        
        # Plot 3: 20x20
        cut20 = Cutout2D(data, (target_x, target_y), (20, 20), wcs=wcs)
        axs[2].imshow(cut20.data, cmap='viridis', origin='lower', norm=norm80)
        axs[2].plot(10, 10, 'rx', markersize=10)
        axs[2].set_title('20x20 Cutout')
        
        # Plot 4: Local Pixel Histogram
        if local_data is not None:
            axs[3].hist(local_data.flatten(), bins=30, color='blue', alpha=0.7)
            axs[3].axvline(x=g_median, color='red', linestyle='--', label='Global Median')
            axs[3].set_title('Local Pixel Histogram (40x40)')
            axs[3].legend()
            
        plt.suptitle(f"Diagnostic Analysis: {record_id}\nClassification: {classification}")
        plt.tight_layout()
        
        fig_dir = output_dir / "figures"
        fig_dir.mkdir(parents=True, exist_ok=True)
        plt.savefig(fig_dir / f"{record_id}_target_cutout.png", dpi=150)
        plt.close()
        
        # 12. Save JSON
        diag_results = {
            "record_id": record_id,
            "header_info": header_info,
            "precise_footprint": {
                "target_x": target_x,
                "target_y": target_y,
                "is_inside": is_inside
            },
            "local_stats": local_stats,
            "detections": detection_results,
            "final_classification": classification
        }
        
        proc_dir = output_dir / "processed"
        proc_dir.mkdir(parents=True, exist_ok=True)
        with open(proc_dir / f"{record_id}_diagnostic.json", "w") as f:
            json.dump(diag_results, f, indent=2)
            
        print("Diagnostic Complete.")
        print(json.dumps(diag_results, indent=2))
        
if __name__ == "__main__":
    output_dir = Path(r"E:\Downloads\ASTRION\data")
    fits_path = output_dir / "raw" / "ztf" / "ztf_20200808235336_001573_c06_q1_cutout.fits"
    prov_path = output_dir / "metadata" / "ztf_20200808235336_001573_c06_q1_prov.json"
    
    diagnostic_analysis(fits_path, prov_path, output_dir)
