# INTERNAL STATUS REPORT - DATASET STATE

## Existing Files
*   `data/raw/ztf`: 49 FITS files (41 original C/2020 F3 crops + 8 Ceres/Eros control crops)
*   `data/dataset_v1/manifest.json`: Contains 2 successful CONFIRMED_POSITIVE exact-time Ceres samples before IRSA cutout endpoint failed.

## Current Dataset Status
*   **Valid Samples:** 2 (from exact-time V1 generation) + ~8 controls
*   **Uncertain/Unverified:** 41 C/2020 F3 images. These lack exact-time localization (were pulled from coarse 0.1 deg center) so they are currently flagged `UNVERIFIED_PENDING_RELOCALIZATION`.
*   **Dataset Generator:** Currently broken for large scale because the IPAC IRSA cutout endpoint throws HTTP 500 when requested aggressively or for specific small-body configurations.

## Action Plan
1.  Replace `retrieve_data` in `ztf.py` to download the full ZTF quadrant image (~37 MB) and store it in `data/cache/ztf_science/`.
2.  Rewrite `dataset_generator.py` to load the full cached FITS, query exact Horizons epoch, use `Cutout2D` locally (64x64 or 128x128 pixels), and run the same DAOStarFinder/segmentation pipeline.
3.  Execute a 5-sample verification.
4.  Run the full 150-sample loop over multiple Solar System objects automatically.
