# Evidence Resolution

The Evidence Resolver (`src/resolver.py`) is the core engine of ASTRION. It binds theoretical predictions to empirical observations.

## The Resolution Process

1. **Prediction Generation:** Obtain a time-series of predicted RA/Dec positions from a trusted ephemeris source (JPL Horizons).
2. **Spatial Query:** For each prediction, query image archives (like ZTF) for observations covering that specific RA/Dec.
3. **Temporal Filtering:** Calculate $\Delta t = |t_{obs} - t_{pred}|$. Only retain observations where $\Delta t < tolerance$.
4. **Candidate Generation:** If an observation passes the spatiotemporal filter, it becomes a `CANDIDATE`.

## Strict Validation
A candidate is NOT a confirmation. 
* A telescope might have pointed at the right spot at the right time, but the object might have been too faint.
* The image might be corrupted.
* The object might be obscured by a star or artifact.

Therefore, the resolver explicitly differentiates between:
* `observations_found` (Candidates)
* `validated_observations` (Requires source extraction/human verification)
* `coverage_gaps` (Explicit abstention when no data exists)

## Handling Coverage Gaps
If no archival image overlaps the predicted coordinate within the allowed time window, the system registers a `coverage_gap`. It does **not** attempt to hallucinate an observation or substitute a wildly distant image.
