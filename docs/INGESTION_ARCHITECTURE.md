# Ingestion Architecture

ASTRION uses a lightweight plugin architecture for data ingestion to handle the disparate nature of astronomical APIs while preserving strict scientific provenance.

## Core Design
The architecture revolves around a `ConnectorRegistry` and a `BaseIngestionConnector`.

Each connector MUST implement:
1. `source_id`: A canonical identifier (e.g., `irsa_ztf`).
2. `capabilities`: A dictionary declaring what the source provides (images, catalogs, ephemerides).
3. `query_metadata(...)`: To search for data without downloading large payloads.
4. `retrieve_data(...)`: To fetch small targeted datasets (e.g., FITS cutouts).

## Current Connectors
* **JPL Horizons (`jpl_horizons.py`):** Wraps `astroquery.jplhorizons` to generate predicted positions.
* **ZTF (`ztf.py`):** Uses the IRSA API to find intersecting science images.

## TODO Connectors
* **MAST (`mast.py`)**
* **SIMBAD (`simbad.py`)**
Connectors that are not yet verified are marked as `NotImplementedError` rather than fabricating responses.

## Principles
1. **Never download massive datasets:** Always query metadata first.
2. **Preserve exact URIs:** We must know where every FITS file or catalog row came from.
3. **No silent fallbacks:** If a connector fails, it must raise a visible error or return a documented failure state.
