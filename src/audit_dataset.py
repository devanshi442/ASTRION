import json
import os
import hashlib
from pathlib import Path
import numpy as np
import matplotlib.pyplot as plt
import pandas as pd
from astropy.io import fits
from astropy.wcs import WCS
from astropy.coordinates import SkyCoord
import astropy.units as u

def get_hash(filepath):
    h = hashlib.sha256()
    with open(filepath, 'rb') as f:
        while chunk := f.read(8192):
            h.update(chunk)
    return h.hexdigest()

def run_audit():
    dataset_dir = Path("E:/Downloads/ASTRION/data/dataset_v1")
    eda_dir = Path("E:/Downloads/ASTRION/data/eda_v1")
    eda_dir.mkdir(parents=True, exist_ok=True)

    man_path = dataset_dir / "manifest.json"

    with open(man_path, "r") as f:
        manifest = json.load(f)

    print(f"Loaded {len(manifest)} manifest entries.")

    # 1. File existence and discrepancies
    fits_files = list(dataset_dir.rglob("*.fits"))
    png_files = list(dataset_dir.rglob("*.png"))

    print(f"Found {len(fits_files)} FITS files.")
    print(f"Found {len(png_files)} PNG files.")

    manifest_fits_paths = [dataset_dir / m["fits_path"] for m in manifest]

    missing_fits = [p for p in manifest_fits_paths if not p.exists()]
    untracked_fits = [p for p in fits_files if p not in manifest_fits_paths]

    # Analyze duplicates
    record_ids = [m["record_id"] for m in manifest]
    dup_records = set([x for x in record_ids if record_ids.count(x) > 1])

    hashes = {}
    dup_hashes = []
    for f_path in fits_files:
        h = get_hash(f_path)
        if h in hashes:
            dup_hashes.append((f_path, hashes[h]))
        else:
            hashes[h] = f_path

    # Audit each sample for scientific validity
    validity_results = []
    for m in manifest:
        f_path = dataset_dir / m["fits_path"]
        res = {
            "record_id": m["record_id"],
            "object": m["object"],
            "label": m["label"],
            "fits_exists": f_path.exists(),
            "readable": False,
            "has_nans": False,
            "has_infs": False,
            "inside_footprint": False,
            "wcs_valid": False,
            "dim_x": None,
            "dim_y": None,
            "questionable_label": False
        }

        if f_path.exists():
            try:
                with fits.open(f_path) as hdul:
                    hdu = hdul[0]
                    res["readable"] = True
                    res["dim_y"], res["dim_x"] = hdu.data.shape
                    res["has_nans"] = np.isnan(hdu.data).any()
                    res["has_infs"] = np.isinf(hdu.data).any()

                    try:
                        wcs = WCS(hdu.header)
                        res["wcs_valid"] = True

                        target_sky = SkyCoord(ra=m["ra"]*u.deg, dec=m["dec"]*u.deg, frame='icrs')
                        px, py = wcs.world_to_pixel(target_sky)
                        px, py = float(px), float(py)

                        if 0 <= px < res["dim_x"] and 0 <= py < res["dim_y"]:
                            res["inside_footprint"] = True

                    except Exception as e:
                        pass
            except Exception:
                pass

        # Flag questionable labels (e.g. separation > 1.5 but labeled positive?)
        # Or if SNR is low.
        if m["label"] == "CONFIRMED_POSITIVE":
            if m["angular_separation"] > 3.0 or m["snr"] < 10.0:
                res["questionable_label"] = True

        validity_results.append(res)

    df = pd.DataFrame(manifest)
    df_val = pd.DataFrame(validity_results)

    # Output to EDA directory
    df.to_csv(eda_dir / "dataset_manifest_export.csv", index=False)
    df_val.to_csv(eda_dir / "dataset_validity.csv", index=False)

    # Plotting
    plt.figure(figsize=(10, 6))
    df['label'].value_counts().plot(kind='bar', color=['skyblue', 'salmon'])
    plt.title("Label Distribution")
    plt.ylabel("Count")
    plt.tight_layout()
    plt.savefig(eda_dir / "label_distribution.png")

    plt.figure(figsize=(12, 6))
    df.groupby(['object', 'label']).size().unstack(fill_value=0).plot(kind='bar', stacked=True)
    plt.title("Per-Object Label Distribution")
    plt.ylabel("Count")
    plt.tight_layout()
    plt.savefig(eda_dir / "object_distribution.png")

    if 'obsjd' in df.columns:
        plt.figure(figsize=(12, 6))
        for obj in df['object'].unique():
            obj_data = df[df['object'] == obj]
            plt.scatter(obj_data['obsjd'], [obj]*len(obj_data), alpha=0.6, label=obj)
        plt.title("Observation Date Coverage")
        plt.xlabel("JD")
        plt.tight_layout()
        plt.savefig(eda_dir / "date_coverage.png")

    # Write report
    report = []
    report.append("# ASTRION Dataset V1 EDA & Audit Report\n")
    report.append(f"**Total Manifest Entries:** {len(manifest)}")
    report.append(f"**FITS Files on Disk:** {len(fits_files)}")
    report.append(f"**PNG Files on Disk:** {len(png_files)}")
    report.append(f"\n## Discrepancies")
    report.append(f"- Missing FITS files (in manifest but not on disk): {len(missing_fits)}")
    report.append(f"- Untracked FITS files (on disk but not in manifest): {len(untracked_fits)}")
    report.append(f"- Duplicate Record IDs in manifest: {len(dup_records)}")
    report.append(f"- Duplicate FITS Hashes: {len(dup_hashes)}")

    report.append(f"\n## Scientific Validity")
    num_unreadable = len(df_val[~df_val['readable']])
    num_out_of_bounds = len(df_val[~df_val['inside_footprint']])
    num_invalid_wcs = len(df_val[~df_val['wcs_valid']])
    num_questionable = len(df_val[df_val['questionable_label']])

    report.append(f"- Unreadable FITS: {num_unreadable}")
    report.append(f"- Invalid WCS: {num_invalid_wcs}")
    report.append(f"- Predicted Target Out-of-Bounds: {num_out_of_bounds}")
    report.append(f"- Questionable Labels (Positives with high sep or low SNR): {num_questionable}")

    with open(eda_dir / "EDA_REPORT.md", "w") as f:
        f.write("\n".join(report))

    print(f"EDA and Audit complete. Outputs written to {eda_dir}")

if __name__ == '__main__':
    run_audit()
