# DATASET V1 PILOT - FINAL AUDIT REPORT

## 1. Quality Control & Validation
- **Total Valid Samples:** 1
- **Positives (CONFIRMED_POSITIVE):** 1
- **Negatives (HARD_NEGATIVE):** 0
- **Uncertain (HUMAN_VERIFICATION_REQUIRED):** 0
- **Invalid:** 0 (Filtered during generation)

## 2. Splits & Composition
- **Unique Objects:** 1
- **Unique Science Images:** 1
- **Train Objects:** 99942 Apophis
- **Validation Objects:** None
- **Test Objects:** None

## 3. Methodology
- All cutouts generated via local full-FITS caching and exact-epoch `Cutout2D` extraction.
- Label methodology: Astropy/Photutils Segmentation on WCS-aligned exact ephemeris targets. Strict SNR/Separation logic to separate stars from true moving objects.
- Missing Provenance Count: 0 (Strict validation before manifest write).
- Duplicate Count: 0 (Strict set tracking).

## 4. Distributions
### Negative Reasons

### Filters
- ZTF_r: 1

### Metrics
- **SNR:** min=10.0, median=10.0, max=10.0
- **Angular Separation:** min=1.00", median=1.00", max=1.00"
- **Observation Date Range (JD):** 2458649.00 to 2458649.00

## 5. Observations by Object
- **99942 Apophis:** 1 samples