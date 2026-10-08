import os
import json
import numpy as np
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from pathlib import Path
from astropy.io import fits
from astropy.wcs import WCS
from astropy.coordinates import SkyCoord
import astropy.units as u
from photutils.background import Background2D, MedianBackground
from photutils.detection import DAOStarFinder
from astropy.stats import sigma_clipped_stats

def analyze_image(fits_path: Path, prov_path: Path, output_dir: Path):
    print(f"Loading provenance from {prov_path}")
    with open(prov_path, "r") as f:
        prov = json.load(f)
        
    cand_info = prov.get("candidate_info", {})
    pred = cand_info.get("prediction", {})
    
    target_ra = pred.get("RA")
    target_dec = pred.get("DEC")
    record_id = prov.get("record_id")
    
    if target_ra is None or target_dec is None:
        raise ValueError("Missing predicted RA/DEC in provenance")
        
    print(f"Loading FITS from {fits_path}")
    with fits.open(fits_path) as hdul:
        hdu = hdul[0]
        data = np.asarray(hdu.data, dtype=float)
        header = hdu.header
        
        # Validation
        if data is None or data.ndim != 2:
            raise ValueError("Primary HDU does not contain a 2D image")
            
        wcs = WCS(header)
        wcs_valid = wcs.has_celestial
        
        # Dimensions
        ny, nx = data.shape
        print(f"Image dimensions: {ny}x{nx}")
        
        # Pixel scale
        pixel_scale_deg = None
        if wcs_valid:
            pixel_scale_deg = np.mean(astropy.wcs.utils.proj_plane_pixel_scales(wcs))
            pixel_scale_arcsec = pixel_scale_deg * 3600.0
            print(f"Pixel scale: {pixel_scale_arcsec:.3f} arcsec/pixel")
        else:
            print("WCS invalid, cannot determine pixel scale.")
            
        # Predicted coords
        pred_x, pred_y = wcs.world_to_pixel_values(target_ra, target_dec)
        is_inside = (0 <= pred_x < nx) and (0 <= pred_y < ny)
        print(f"Predicted RA/DEC: {target_ra:.6f}, {target_dec:.6f}")
        print(f"Predicted pixel coords: x={pred_x:.2f}, y={pred_y:.2f}")
        print(f"Prediction inside image: {is_inside}")
        
        # Background estimation
        bkg_estimator = MedianBackground()
        bkg = Background2D(data, (32, 32), filter_size=(3, 3), bkg_estimator=bkg_estimator)
        data_sub = data - bkg.background
        
        mean, median, std = sigma_clipped_stats(data_sub, sigma=3.0)
        print(f"Background statistics: mean={mean:.3f}, median={median:.3f}, std={std:.3f}")
        
        # Source detection
        threshold = 5.0 * std
        print(f"Detection threshold: {threshold:.3f}")
        
        daofind = DAOStarFinder(fwhm=3.0, threshold=threshold)
        sources = daofind(data_sub)
        
        num_sources = len(sources) if sources is not None else 0
        print(f"Number of detected sources: {num_sources}")
        
        classification = "UNCERTAIN"
        nearest_sep_arcsec = None
        nearest_source = None
        
        if not is_inside:
            classification = "NOT_IN_IMAGE"
        elif num_sources == 0:
            classification = "NO_CANDIDATE"
        else:
            # calculate separations
            source_coords = wcs.pixel_to_world_values(sources['xcentroid'], sources['ycentroid'])
            source_ras, source_decs = source_coords[0], source_coords[1]
            
            sc_target = SkyCoord(ra=target_ra*u.deg, dec=target_dec*u.deg, frame='icrs')
            sc_sources = SkyCoord(ra=source_ras*u.deg, dec=source_decs*u.deg, frame='icrs')
            
            separations = sc_target.separation(sc_sources).arcsec
            nearest_idx = np.argmin(separations)
            nearest_sep_arcsec = float(separations[nearest_idx])
            nearest_source = {
                "id": int(sources['id'][nearest_idx]),
                "x": float(sources['xcentroid'][nearest_idx]),
                "y": float(sources['ycentroid'][nearest_idx]),
                "ra": float(source_ras[nearest_idx]),
                "dec": float(source_decs[nearest_idx]),
                "flux": float(sources['flux'][nearest_idx]),
                "separation_arcsec": nearest_sep_arcsec
            }
            
            print(f"Nearest detected source: ID={nearest_source['id']} at ({nearest_source['ra']:.5f}, {nearest_source['dec']:.5f})")
            print(f"Nearest-source angular separation: {nearest_sep_arcsec:.3f} arcsec")
            
            # search radius: 5 arcseconds
            if nearest_sep_arcsec <= 5.0:
                classification = "CANDIDATE"
            else:
                classification = "NO_CANDIDATE"
                
        print(f"\nFinal Classification: {classification}\n")
        
        # Save output table
        proc_dir = output_dir / "processed"
        proc_dir.mkdir(parents=True, exist_ok=True)
        if num_sources > 0:
            sources.write(proc_dir / f"{record_id}_sources.ecsv", format='ascii.ecsv', overwrite=True)
            
        # Save analysis JSON
        results = {
            "record_id": record_id,
            "predicted_ra": target_ra,
            "predicted_dec": target_dec,
            "predicted_x": float(pred_x),
            "predicted_y": float(pred_y),
            "is_inside": bool(is_inside),
            "wcs_valid": bool(wcs_valid),
            "image_dimensions": [ny, nx],
            "pixel_scale_arcsec": float(pixel_scale_arcsec) if pixel_scale_arcsec else None,
            "background_stats": {"mean": float(mean), "median": float(median), "std": float(std)},
            "detection_threshold": float(threshold),
            "num_sources": num_sources,
            "nearest_source": nearest_source,
            "classification": classification
        }
        
        with open(proc_dir / f"{record_id}_analysis.json", "w") as f:
            json.dump(results, f, indent=2)
            
        # Plotting
        fig_dir = output_dir / "figures"
        fig_dir.mkdir(parents=True, exist_ok=True)
        
        plt.figure(figsize=(8,8))
        norm = matplotlib.colors.Normalize(vmin=median - std, vmax=median + 5*std)
        plt.imshow(data, cmap='viridis', origin='lower', norm=norm)
        
        if num_sources > 0:
            plt.scatter(sources['xcentroid'], sources['ycentroid'], facecolors='none', edgecolors='blue', s=30, label='Sources')
            
        if is_inside:
            plt.scatter(pred_x, pred_y, marker='x', color='red', s=100, label='Predicted Position')
            
            if pixel_scale_arcsec:
                # 5 arcsec search radius in pixels
                radius_px = 5.0 / pixel_scale_arcsec
                circle = plt.Circle((pred_x, pred_y), radius_px, color='red', fill=False, linestyle='--', label='5" Search Radius')
                plt.gca().add_patch(circle)
                
        plt.title(f"ASTRION Phase 2 Analysis\n{record_id}\nClass: {classification}")
        plt.legend()
        plt.savefig(fig_dir / f"{record_id}_analysis.png", dpi=150)
        plt.close()

if __name__ == "__main__":
    import astropy
    import astropy.wcs.utils
    
    output_dir = Path(r"E:\Downloads\ASTRION\data")
    fits_path = output_dir / "raw" / "ztf" / "ztf_20200808235336_001573_c06_q1_cutout.fits"
    prov_path = output_dir / "metadata" / "ztf_20200808235336_001573_c06_q1_prov.json"
    
    analyze_image(fits_path, prov_path, output_dir)
