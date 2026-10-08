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
from astropy.stats import sigma_clipped_stats
from photutils.detection import DAOStarFinder
from photutils.segmentation import detect_sources, SourceCatalog
from astropy.convolution import Gaussian2DKernel

from src.ingestion.registry import ConnectorRegistry

def run_exact_control_experiment(output_dir: Path):
    jpl = ConnectorRegistry.get_connector("jpl_horizons")
    ztf = ConnectorRegistry.get_connector("irsa_ztf")
    
    target = "433"
    start = "2020-09-01"
    stop = "2020-09-02"
    step = "12h"
    
    print(f"Querying Horizons for coarse trajectory of {target}...")
    from astroquery.jplhorizons import Horizons
    obj_coarse = Horizons(id=target, id_type="smallbody", location="I41",
                          epochs={"start": start, "stop": stop, "step": step})
    eph = obj_coarse.ephemerides()
    eph_data = []
    for row in eph:
        eph_data.append({
            "datetime_jd": float(row["datetime_jd"]),
            "RA": float(row["RA"]),
            "DEC": float(row["DEC"]),
        })
        
    candidates = []
    downloaded = 0
    max_downloads = 3
    
    raw_dir = output_dir / "raw" / "ztf"
    raw_dir.mkdir(parents=True, exist_ok=True)
    
    for eph in eph_data:
        if downloaded >= max_downloads:
            break
            
        ztf_obs = ztf.query_metadata(ra=eph["RA"], dec=eph["DEC"], radius_deg=1.0)
        
        for obs in ztf_obs:
            if downloaded >= max_downloads:
                break
                
            t_sep_days = abs(obs["obsjd"] - eph["datetime_jd"])
            if t_sep_days <= 1.0:
                record_id = obs["record_id"]
                fits_path = raw_dir / f"{record_id}_control.fits"
                
                # 1. Exact Observation Time from IRSA metadata
                obsjd_exact = float(obs["obsjd"])
                
                # 2. Exact JPL Horizons Position
                print(f"Querying exact ephemeris for JD {obsjd_exact}...")
                from astroquery.jplhorizons import Horizons
                obj_exact = Horizons(id=target, id_type="smallbody", location="I41", epochs=[obsjd_exact])
                exact_eph_table = obj_exact.ephemerides()
                
                if not exact_eph_table:
                    continue
                    
                exact_eph_row = exact_eph_table[0]
                pred_ra = float(exact_eph_row["RA"])
                pred_dec = float(exact_eph_row["DEC"])
                ra_rate_hr = float(exact_eph_row["RA_rate"]) if "RA_rate" in exact_eph_table.colnames else 0.0
                dec_rate_hr = float(exact_eph_row["DEC_rate"]) if "DEC_rate" in exact_eph_table.colnames else 0.0
                exact_jd_used = float(exact_eph_row["datetime_jd"])
                
                # 3. Retrieve FITS centered precisely on EXACT prediction
                fits_path = raw_dir / f"{record_id}_exact.fits"
                print(f"Retrieving/Locating EXACT FITS {record_id}...")
                if not fits_path.exists():
                    r = ztf.retrieve_data(record_id, original_uri=obs["original_uri"], 
                                          ra=pred_ra, dec=pred_dec, size=0.1)
                    if r.status_code == 200:
                        with open(fits_path, "wb") as f:
                            f.write(r.content)
                    else:
                        continue
                
                try:
                    with fits.open(fits_path) as hdul:
                        hdu = hdul[0]
                        data = np.asarray(hdu.data, dtype=float)
                        header = hdu.header
                        wcs = WCS(header)
                        wcs_valid = wcs.has_celestial
                        exptime = float(header.get("EXPTIME", 30.0))
                        
                        # Calculate trail length
                        total_rate_arcsec_sec = np.sqrt(ra_rate_hr**2 + dec_rate_hr**2) / 3600.0
                        trail_length_arcsec = total_rate_arcsec_sec * exptime
                        
                        pixel_scales = astropy.wcs.utils.proj_plane_pixel_scales(wcs) * 3600.0
                        pixel_scale = float(pixel_scales[0])
                        trail_length_pixels = trail_length_arcsec / pixel_scale
                        
                        # 3. Exact WCS Projection
                        target_sky = SkyCoord(ra=pred_ra*u.deg, dec=pred_dec*u.deg, frame='icrs')
                        pred_x, pred_y = wcs.world_to_pixel(target_sky)
                        pred_x, pred_y = float(pred_x), float(pred_y)
                        
                        # inside image check
                        ny, nx = data.shape
                        inside_image = (0 <= pred_x < nx) and (0 <= pred_y < ny)
                        
                        # Local stats
                        mean, median, std = sigma_clipped_stats(data, sigma=3.0)
                        
                        # 5. Detection Methods
                        # A. DAOStarFinder
                        daofind = DAOStarFinder(fwhm=3.0, threshold=median + 5.0*std)
                        dao_sources = daofind(data - median)
                        dao_nearest = 999.0
                        dao_detected = False
                        dao_snr = 0.0
                        
                        if dao_sources is not None and len(dao_sources) > 0:
                            dao_sc = wcs.pixel_to_world_values(dao_sources['xcentroid'], dao_sources['ycentroid'])
                            dao_sky = SkyCoord(ra=dao_sc[0]*u.deg, dec=dao_sc[1]*u.deg, frame='icrs')
                            dao_seps = target_sky.separation(dao_sky).arcsec
                            dao_nearest = float(np.min(dao_seps))
                            if dao_nearest <= 10.0:
                                dao_detected = True
                                idx = np.argmin(dao_seps)
                                dao_snr = float(dao_sources['peak'][idx] / std)
                        
                        # B. Segmentation
                        segm = detect_sources(data, median + 5.0*std, n_pixels=5)
                        seg_nearest = 999.0
                        seg_detected = False
                        seg_area = 0.0
                        seg_snr = 0.0
                        
                        if segm is not None:
                            cat = SourceCatalog(data, segm, wcs=wcs)
                            tbl = cat.to_table()
                            seg_sc = wcs.pixel_to_world_values(tbl['xcentroid'], tbl['ycentroid'])
                            seg_sky = SkyCoord(ra=seg_sc[0]*u.deg, dec=seg_sc[1]*u.deg, frame='icrs')
                            seg_seps = target_sky.separation(seg_sky).arcsec
                            seg_nearest = float(np.min(seg_seps))
                            if seg_nearest <= 10.0:
                                seg_detected = True
                                idx = np.argmin(seg_seps)
                                r = tbl[idx]
                                seg_area = float(r['area'].value) if hasattr(r['area'], 'value') else float(r['area'])
                                seg_snr = float(r['max_value'] / std)
                                
                        candidates.append({
                            "object": "433 Eros",
                            "record_id": record_id,
                            "observation_jd_fits": obsjd_exact,
                            "ephemeris_jd_used": exact_jd_used,
                            "temporal_difference_days": abs(obsjd_exact - exact_jd_used),
                            "filter": header.get("FILTER"),
                            "exposure": exptime,
                            "predicted_ra": pred_ra,
                            "predicted_dec": pred_dec,
                            "predicted_x": pred_x,
                            "predicted_y": pred_y,
                            "wcs_valid": wcs_valid,
                            "inside_image": inside_image,
                            "expected_trail_length_arcsec": trail_length_arcsec,
                            "expected_trail_length_pixels": trail_length_pixels,
                            "dao_angular_separation": dao_nearest,
                            "dao_detection_snr": dao_snr,
                            "dao_detected": dao_detected,
                            "seg_angular_separation": seg_nearest,
                            "seg_detection_snr": seg_snr,
                            "seg_morphology_area": seg_area,
                            "seg_detected": seg_detected,
                            "local_background_std": float(std)
                        })
                        
                        # 7. Diagnostic Figures
                        fig, ax = plt.subplots(figsize=(8,8))
                        norm = matplotlib.colors.Normalize(vmin=median - std, vmax=median + 5*std)
                        ax.imshow(data, cmap='viridis', origin='lower', norm=norm)
                        
                        ax.plot(pred_x, pred_y, 'rx', markersize=15, label="Exact Predicted Position")
                        
                        if dao_sources is not None:
                            ax.scatter(dao_sources['xcentroid'], dao_sources['ycentroid'], facecolors='none', edgecolors='blue', s=20, alpha=0.5, label="DAO Detections")
                            
                        if segm is not None:
                            ax.scatter(tbl['xcentroid'], tbl['ycentroid'], facecolors='none', edgecolors='red', s=60, alpha=0.8, label="Seg Detections")
                            
                        ax.set_title(f"Exact Localization: 433 Eros\nObs: {record_id}\nOBSJD: {obsjd_exact}")
                        ax.legend()
                        
                        fig_dir = output_dir / "figures"
                        fig_dir.mkdir(parents=True, exist_ok=True)
                        plt.savefig(fig_dir / f"exact_control_{record_id}.png", dpi=150)
                        plt.close()
                        
                        downloaded += 1
                        
                except Exception as e:
                    print(f"Error processing {record_id}: {e}")
                
    proc_dir = output_dir / "processed"
    proc_dir.mkdir(parents=True, exist_ok=True)
    with open(proc_dir / "exact_control_results.json", "w") as f:
        json.dump(candidates, f, indent=2)
        
    print(json.dumps(candidates, indent=2))

if __name__ == "__main__":
    out_dir = Path(r"E:\Downloads\ASTRION\data")
    run_exact_control_experiment(out_dir)
