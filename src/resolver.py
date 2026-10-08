from typing import Dict, Any, List, Optional
import numpy as np
from datetime import datetime, timezone

from .ingestion.registry import ConnectorRegistry

class EvidenceResolver:
    """
    Evidence Resolver module.
    Responsible for binding predicted positions to archival observations,
    applying scientific validation rules (temporal and spatial tolerances),
    and strictly differentiating predictions from confirmed targets.
    """
    
    def __init__(self):
        self.horizons = ConnectorRegistry.get_connector("jpl_horizons")
        self.ztf = ConnectorRegistry.get_connector("irsa_ztf")
        
    def resolve(self, 
                object_designation: str, 
                start_time: str, 
                end_time: str,
                step: str = "1d",
                temporal_tolerance_days: float = 0.5) -> Dict[str, Any]:
        
        result = {
            "sources_checked": ["jpl_horizons", "irsa_ztf"],
            "observations_found": 0,
            "observations_not_found": 0,
            "coverage_gaps": [],
            "candidate_observations": [],
            "validated_observations": [], # Empty because we don't download/extract sources here
            "failed_observations": [],
            "provenance": {
                "resolver_version": "0.1",
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }
        }
        
        # 1. Obtain ephemeris from JPL Horizons
        ephemerides = self.horizons.query_metadata(
            target=object_designation,
            start=start_time,
            stop=end_time,
            step=step
        )
        
        if not ephemerides:
            result["failed_observations"].append("Could not retrieve ephemerides.")
            return result
            
        # 2. Query archives (ZTF) for each predicted position
        for eph in ephemerides:
            ra = eph["RA"]
            dec = eph["DEC"]
            jd_target = eph["datetime_jd"]
            
            # Query ZTF
            ztf_metadata = self.ztf.query_metadata(ra=ra, dec=dec)
            
            if not ztf_metadata:
                result["coverage_gaps"].append({
                    "jd": jd_target,
                    "ra": ra,
                    "dec": dec,
                    "reason": "No spatial coverage in ZTF"
                })
                result["observations_not_found"] += 1
                continue
                
            # Filter by temporal proximity
            jds = np.array([m["obsjd"] for m in ztf_metadata])
            delta_days = np.abs(jds - jd_target)
            
            valid_indices = np.where(delta_days <= temporal_tolerance_days)[0]
            
            if len(valid_indices) == 0:
                result["coverage_gaps"].append({
                    "jd": jd_target,
                    "ra": ra,
                    "dec": dec,
                    "reason": f"No temporal coverage in ZTF within {temporal_tolerance_days} days."
                })
                result["observations_not_found"] += 1
                continue
                
            from astropy.coordinates import SkyCoord
            import astropy.units as u

            # Create candidates
            for idx in valid_indices:
                meta = ztf_metadata[idx]
                
                # Accurate angular separation using SkyCoord
                obs_ra = meta.get("ra", ra)
                obs_dec = meta.get("dec", dec)
                
                coord_pred = SkyCoord(ra=ra*u.deg, dec=dec*u.deg, frame='icrs')
                coord_obs = SkyCoord(ra=obs_ra*u.deg, dec=obs_dec*u.deg, frame='icrs')
                
                spatial_sep_arcsec = coord_pred.separation(coord_obs).arcsec

                candidate = {
                    "resolution_status": "CANDIDATE",
                    "prediction": eph,
                    "observation_timestamp_jd": float(meta["obsjd"]),
                    "source_record_id": meta["record_id"],
                    "source_uri": meta.get("original_uri"),
                    "temporal_separation_days": float(delta_days[idx]),
                    "spatial_separation_arcsec": float(spatial_sep_arcsec),
                    "source_archive": "irsa_ztf",
                    "note": "Observation spatially and temporally overlaps prediction, but object presence is not human-verified."
                }
                result["candidate_observations"].append(candidate)
                result["observations_found"] += 1
                
        return result
