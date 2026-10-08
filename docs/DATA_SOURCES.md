# ASTRION Data Sources

This document describes the canonical data sources researched for the ASTRION project, their capabilities, limitations, and how they integrate into our pipelines. The machine-readable equivalent is `data_sources.yaml`.

## 1. Ephemerides & Orbital Dynamics
### JPL Horizons
* **Use Case:** Solar System ephemerides.
* **Capabilities:** Provides predicted astrometry (RA/Dec), geometry, distances, and rates.
* **Limitations:** Horizons is a *calculator*, not an observation archive. It does not contain FITS images or actual measurements, only numerical predictions.

## 2. Image Archives
### IRSA / ZTF (Zwicky Transient Facility)
* **Use Case:** Time-domain imaging and transient detection.
* **Capabilities:** Public DRs contain FITS cutouts and catalogs searchable by RA/Dec and time (POS/INTERSECT).
* **Limitations:** Not full-sky at all times. Significant temporal gaps. Does not accept small body names (e.g. "C/2020 F3") natively; requires ephemeris lookup first.

### MAST (Pan-STARRS)
* **Use Case:** High-quality deep static imaging.
* **Capabilities:** Coverage of the Northern Hemisphere.
* **Limitations:** Not ideal for moving targets due to survey cadence. Rate limits can be strict.

## 3. Object Databases
### SIMBAD
* **Use Case:** Stars, galaxies, exoplanet hosts.
* **Capabilities:** Extensive cross-identification, basic photometry, spectral types.
* **Limitations:** Explicitly *excludes* Solar System objects. Useless for comets/asteroids.

### JPL SBDB
* **Use Case:** Small body physical parameters (albedo, diameter) and orbital elements.
* **Limitations:** Does not generate epoch ephemerides directly (Horizons does that).

## 4. Survey Catalogs
### Gaia (DR3)
* **Use Case:** High-precision astrometry.
* **Capabilities:** Contains some SSO data.
* **Limitations:** API is geared towards asynchronous ADQL jobs, not real-time single-object ephemeris matching.
