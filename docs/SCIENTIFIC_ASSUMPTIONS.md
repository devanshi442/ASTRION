# Scientific Assumptions

To ensure ASTRION remains a scientifically defensible system, the following assumptions and axioms are strictly maintained:

1. **Ephemerides are Predictions:**
   An output from JPL Horizons is a mathematical prediction of where an object *should* be, based on an orbital model. It is not an empirical observation of the object at that time.

2. **Proximity is Not Identity:**
   Finding a point source in a catalog or image at RA=120.0, Dec=+45.0 does not guarantee it is the target object, even if the ephemeris predicts the target to be there. Background stars, artifacts, or other transients could masquerade as the target. A proper validation step (e.g., checking proper motion, cross-matching with static catalogs) is required before a candidate becomes a confirmation.

3. **Data Completeness Cannot be Assumed:**
   If a NASA API (like IRSA ZTF) returns no results for a specific time and place, it means *that specific survey* did not observe it, or the data is proprietary, or the API is failing. It does not necessarily mean the object didn't exist or that *no* telescope observed it. The system must report a "coverage gap".

4. **WCS is Mandatory for Spatial Association:**
   Images without a valid World Coordinate System (WCS) cannot be algorithmically associated with an ephemeris position.

5. **LLM Limitations:**
   LLMs cannot perform reliable precision astrometry, photometry, or complex celestial mechanics calculations natively. All numerical astronomy must be delegated to deterministic scientific libraries (e.g., `astropy`, `numpy`). The LLM acts only as an orchestrator, query planner, and semantic interpreter.
