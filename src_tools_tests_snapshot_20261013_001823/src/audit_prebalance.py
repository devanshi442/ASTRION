import json
from pathlib import Path
from astropy.io import fits
from astropy.wcs import WCS
import numpy as np

def run_audit():
    man_path = Path(r"E:\Downloads\ASTRION\data\dataset_v1\manifest.json")
    if not man_path.exists():
        print("Manifest not found!")
        return

    with open(man_path, 'r') as f:
        manifest = json.load(f)

    valid_count = 0
    invalid_count = 0
    obj_counts = {}
    label_counts = {}
    snrs = []
    seps = []
    filter_counts = {}
    dates = []

    updated_manifest = []

    for m in manifest:
        is_valid = True

        # 1. crop FITS exists
        crop_path = Path(r"E:\Downloads\ASTRION\data\dataset_v1") / m["fits_path"]
        if not crop_path.exists():
            is_valid = False
            m["validation_error"] = "Crop FITS missing"
        else:
            try:
                with fits.open(crop_path) as hdul:
                    data = hdul[0].data
                    header = hdul[0].header
                    wcs = WCS(header)
                    # 3. WCS is valid
                    if not wcs.is_celestial:
                        is_valid = False
                        m["validation_error"] = "Invalid WCS"

                    # 8. predicted position maps inside crop
                    px, py = wcs.world_to_pixel_values(m["ra"], m["dec"])
                    if px < 0 or py < 0 or px >= data.shape[1] or py >= data.shape[0]:
                        is_valid = False
                        m["validation_error"] = "Target outside crop"
            except Exception as e:
                is_valid = False
                m["validation_error"] = f"FITS error: {e}"

        # 4, 5, 6, 9-13 metadata checks
        required_keys = ["obsjd", "ephemeris_jd", "ra", "dec", "snr", "angular_separation", "label", "provenance"]
        for k in required_keys:
            if k not in m or m[k] is None:
                is_valid = False
                m["validation_error"] = f"Missing {k}"

        if abs(m.get("obsjd", 0) - m.get("ephemeris_jd", 999)) > 0.001:
            is_valid = False
            m["validation_error"] = "OBSJD mismatch with ephemeris_jd"

        if is_valid:
            valid_count += 1
            snrs.append(m["snr"])
            seps.append(m["angular_separation"])
            obj_counts[m["object"]] = obj_counts.get(m["object"], 0) + 1

            # Temporary labeling logic audit: If angular separation is extremely low, it might be a star, but actually asteroids move, so separation from expected ephemeris should be very small.
            # But we don't have trail length. Let's just keep the label but we can mark questionable ones later.
            label_counts[m["label"]] = label_counts.get(m["label"], 0) + 1
            filter_counts[m["filter"]] = filter_counts.get(m["filter"], 0) + 1
            dates.append(m["obsjd"])
        else:
            invalid_count += 1
            m["label"] = "REQUIRES_REVIEW"
            label_counts["REQUIRES_REVIEW"] = label_counts.get("REQUIRES_REVIEW", 0) + 1

        updated_manifest.append(m)

    # Output audit
    report = [
        "# DATASET V1 PRE-BALANCE AUDIT",
        "",
        f"- **Total Samples:** {len(manifest)}",
        f"- **Passing Validation:** {valid_count}",
        f"- **Failing Validation (REQUIRES_REVIEW):** {invalid_count}",
        "",
        "## Samples by Object",
    ]
    for k, v in obj_counts.items():
        report.append(f"- {k}: {v}")

    report.append("\n## Samples by Label")
    for k, v in label_counts.items():
        report.append(f"- {k}: {v}")

    report.append("\n## Metrics Distribution")
    if snrs:
        report.append(f"- **SNR:** min={np.min(snrs):.1f}, median={np.median(snrs):.1f}, max={np.max(snrs):.1f}")
        report.append(f"- **Angular Separation:** min={np.min(seps):.2f}\", median={np.median(seps):.2f}\", max={np.max(seps):.2f}\"")
        report.append(f"- **Date Range (JD):** {np.min(dates):.2f} to {np.max(dates):.2f}")

    report.append("\n## Filters")
    for k, v in filter_counts.items():
        report.append(f"- {k}: {v}")

    with open(r"E:\Downloads\ASTRION\docs\DATASET_V1_PREBALANCE_AUDIT.md", "w") as f:
        f.write("\n".join(report))

    with open(man_path, "w") as f:
        json.dump(updated_manifest, f, indent=2)

    print("Pre-balance audit complete.")

if __name__ == "__main__":
    run_audit()
