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

def run_control_experiment(output_dir: Path):
    jpl = ConnectorRegistry.get_connector("jpl_horizons")
    ztf = ConnectorRegistry.get_connector("irsa_ztf")
    
    # 1. Obtain ephemeris
    target = "433"
    start = "2020-09-01"
    stop = "2020-09-02"
    step = "12h"
    
    print(f"Querying Horizons for {target} directly via astroquery...")
    from astroquery.jplhorizons import Horizons
    obj = Horizons(id=target, id_type="smallbody", location="I41",
                   epochs={"start": start, "stop": stop, "step": step})
    eph = obj.ephemerides()
    
    eph_data = []
    for row in eph:
        eph_data.append({
            "targetname": row["targetname"],
            "datetime_jd": float(row["datetime_jd"]),
            "datetime_str": row["datetime_str"],
            "RA": float(row["RA"]),
            "DEC": float(row["DEC"]),
            "location": "I41",
        })
    
    if not eph_data:
        print("Horizons query failed. Aborting.")
        return
        
    candidates = []
    downloaded = 0
    max_downloads = 3
    
    raw_dir = output_dir / "raw" / "ztf"
    raw_dir.mkdir(parents=True, exist_ok=True)
    
    print("Searching for ZTF observations...")
    for eph in eph_data:
        if downloaded >= max_downloads:
            break
            
        ztf_obs = ztf.query_metadata(ra=eph["RA"], dec=eph["DEC"], radius_deg=1.0)
        
        for obs in ztf_obs:
            t_sep_days = abs(obs["obsjd"] - eph["datetime_jd"])
            if t_sep_days <= 1.0: # Close in time
                record_id = obs["record_id"]
                fits_path = raw_dir / f"{record_id}_control.fits"
                
                print(f"Retrieving FITS {record_id}...")
                if not fits_path.exists():
                    r = ztf.retrieve_data(record_id, original_uri=obs["original_uri"], 
                                          ra=eph["RA"], dec=eph["DEC"], size=0.1)
                    if r.status_code == 200:
                        with open(fits_path, "wb") as f:
                            f.write(r.content)
                    else:
                        continue
                        
                # Analyze image
                try:
                    with fits.open(fits_path) as hdul:
                        hdu = hdul[0]
                        data = np.asarray(hdu.data, dtype=float)
                        header = hdu.header
                        wcs = WCS(header)
                        wcs_valid = wcs.has_celestial
                        
                        target_sky = SkyCoord(ra=eph["RA"]*u.deg, dec=eph["DEC"]*u.deg, frame='icrs')
                        pred_x, pred_y = wcs.world_to_pixel(target_sky)
                        pred_x, pred_y = float(pred_x), float(pred_y)
                        
                        mean, median, std = sigma_clipped_stats(data, sigma=3.0)
                        
                        # DAOStarFinder
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
                        
                        # Segmentation
                        kernel = Gaussian2DKernel(x_stddev=2.0)
                        kernel.normalize()
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
                            "observation_time": obs["obsjd"],
                            "filter": header.get("FILTER"),
                            "exposure": header.get("EXPTIME"),
                            "predicted_ra": eph["RA"],
                            "predicted_dec": eph["DEC"],
                            "predicted_x": pred_x,
                            "predicted_y": pred_y,
                            "angular_separation": min(dao_nearest, seg_nearest),
                            "detection_snr": max(dao_snr, seg_snr),
                            "morphology_area": seg_area,
                            "dao_detected": dao_detected,
                            "seg_detected": seg_detected,
                            "wcs_valid": wcs_valid
                        })
                        
                        # Plot
                        fig, ax = plt.subplots(figsize=(8,8))
                        norm = matplotlib.colors.Normalize(vmin=median - std, vmax=median + 5*std)
                        ax.imshow(data, cmap='viridis', origin='lower', norm=norm)
                        ax.plot(pred_x, pred_y, 'rx', markersize=15, label="Predicted Ceres Position")
                        
                        if segm is not None:
                            ax.scatter(tbl['xcentroid'], tbl['ycentroid'], facecolors='none', edgecolors='red', s=40, label="Segmentation Detections")
                            
                        ax.set_title(f"Control Experiment: 1 Ceres\nObs: {record_id}")
                        ax.legend()
                        
                        fig_dir = output_dir / "figures"
                        fig_dir.mkdir(parents=True, exist_ok=True)
                        plt.savefig(fig_dir / f"control_{record_id}.png", dpi=150)
                        plt.close()
                        
                        downloaded += 1
                        
                except Exception as e:
                    print(f"Error processing {record_id}: {e}")
                
                break # Move to next ephemeris after finding one good image
                
    proc_dir = output_dir / "processed"
    proc_dir.mkdir(parents=True, exist_ok=True)
    with open(proc_dir / "control_object_results.json", "w") as f:
        json.dump(candidates, f, indent=2)
        
    print(json.dumps(candidates, indent=2))

if __name__ == "__main__":
    out_dir = Path(r"E:\Downloads\ASTRION\data")
    run_control_experiment(out_dir)
