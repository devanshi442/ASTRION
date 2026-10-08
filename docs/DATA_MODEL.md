# ASTRION Canonical Data Model

## Introduction
The ASTRION data model enforces a strict separation between **predictions** (ephemerides, models) and **observations** (measurements, images). A coordinate cannot be labeled as a confirmed target merely because it is nearby in space and time; human or robust algorithmic verification is required.

## Core Entities

### 1. Predicted Position (`PredictedPosition`)
Represents where an object *should* be according to orbital dynamics models.
*   **Target Designation:** e.g., `C/2020 F3`
*   **Ephemeris Source:** e.g., `JPL Horizons`
*   **Timestamp:** ISO 8601 (or JD) of the prediction
*   **Observer Location:** e.g., `I41` (Palomar/ZTF)
*   **RA (deg):** Right Ascension (ICRF/J2000)
*   **Dec (deg):** Declination (ICRF/J2000)
*   **Uncertainty (RA/Dec):** 3-sigma error bounds (if available)

### 2. Observation Candidate (`ObservationCandidate`)
Represents an actual data product (e.g., a FITS image) retrieved from an archive that *temporally and spatially overlaps* with the predicted position.
*   **Source Archive:** e.g., `IRSA`
*   **Source Dataset:** e.g., `ZTF sci`
*   **Original URI:** The URL or path to download the product
*   **Retrieval Timestamp:** When the data was queried by ASTRION
*   **Observation Timestamp (JD/MJD):** Start time of the exposure
*   **Instrument/Telescope:** e.g., `ZTF` / `Palomar 48-inch`
*   **Filter/Band:** e.g., `r`
*   **Exposure Time (s):** e.g., `30.0`
*   **WCS Presence:** `True`/`False`
*   **Pixel Scale:** arcsec/pixel
*   **Quality Flags:** Bad pixels, saturation, etc.
*   **Checksum:** SHA-256 of the downloaded file

### 3. Resolution Link (`ResolutionLink`)
The core joining entity that binds a `PredictedPosition` to an `ObservationCandidate` to evaluate their scientific match.
*   **Temporal Separation (days):** Difference between prediction time and exposure time.
*   **Spatial Separation (arcsec/pixels):** Distance between the predicted RA/Dec and the center of the observation, or position in pixels.
*   **Predicted Target in Field:** `True`/`False` (Does the WCS footprint enclose the predicted RA/Dec?)
*   **Validation Status:**
    *   `CANDIDATE`: Overlaps spatially and temporally, but target presence not verified.
    *   `CONFIRMED_DETECTION`: Target successfully extracted or human-verified at the coordinates.
    *   `NON_DETECTION`: Upper limit only; target not visible at predicted location.
    *   `REJECTED_METADATA`: Image corrupted, missing WCS, or other fatal flaw.

## Separation of Concepts
1.  **Prediction $\neq$ Observation:** An ephemeris from Horizons is a mathematical prediction, NOT an observation.
2.  **Archival Image $\neq$ Confirmed Target:** An image retrieved from ZTF that contains the predicted RA/Dec is merely a *candidate*. The target must be confirmed (via visual inspection or source extraction) to be considered an *Identified Object*.
3.  **Observation Date $\neq$ Retrieval Date:** Science uses observation dates (JD/MJD). Retrieval dates only track provenance.

## Provenance
Every observation candidate must carry:
*   `query_parameters`: The exact URL and API arguments used to fetch it.
*   `pipeline_version`: Version of ASTRION that performed the fetch.
*   `license` / `citation`: How to attribute the data source.
