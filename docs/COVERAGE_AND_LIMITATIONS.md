# Coverage and Limitations

## Data Ecosystem Limitations
1. **Archive Fragmentation:** Astronomical data is heavily fragmented. No single API provides access to "all NASA astronomy data". ASTRION currently relies on JPL Horizons for predictions and IRSA ZTF for image candidates.
2. **Proprietary Periods:** Many surveys have 12-18 month proprietary periods. Recent events may have predicted ephemerides but zero public image data.
3. **Temporal Gaps:** Even high-cadence surveys like ZTF have weather losses, maintenance downtime, and seasonal blind spots (e.g., objects too close to the Sun).

## System Limitations
1. **Source Extraction:** Currently, ASTRION can identify *candidate images* that spatially and temporally overlap the target. It does not yet run `photutils` or `SExtractor` to definitively pull the target's PSF out of the noise.
2. **Single Observatory Bias:** The probe primarily tests Palomar (I41) via ZTF. Expanding to space-based observatories requires handling topocentric/geocentric parallax corrections.
3. **Large Datasets:** ASTRION is designed to fetch small cutouts and metadata. It explicitly avoids downloading multi-gigabyte raw FITS cubes. 

## Known Gaps
* The system accurately identifies when ZTF misses an observation (e.g., C/2020 F3 on 2020-07-12). It abstains rather than inventing data.
* Cross-identification via SIMBAD is marked TODO because SIMBAD does not track moving Solar System objects.
