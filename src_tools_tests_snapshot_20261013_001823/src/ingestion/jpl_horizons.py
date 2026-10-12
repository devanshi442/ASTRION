from typing import Any, Dict, List
from .base import BaseIngestionConnector

class JPLHorizonsConnector(BaseIngestionConnector):
    @property
    def source_id(self) -> str:
        return "jpl_horizons"

    @property
    def capabilities(self) -> Dict[str, bool]:
        return {
            "ephemeris": True,
            "image": False,
            "catalog": False,
        }

    def query_metadata(self, **kwargs) -> List[Dict[str, Any]]:
        """
        Query JPL Horizons for ephemerides.
        Expects: target (str), start (str), stop (str), step (str), location (str, default 'I41')
        """
        target = kwargs.get("target")
        start = kwargs.get("start")
        stop = kwargs.get("stop")
        step = kwargs.get("step", "1d")
        location = kwargs.get("location", "I41")

        if not target or not start or not stop:
            raise ValueError("target, start, and stop are required.")

        id_type = kwargs.get("id_type", "designation")
        from astroquery.jplhorizons import Horizons
        
        obj = Horizons(id=target, id_type=id_type, location=location,
                       epochs={"start": start, "stop": stop, "step": step})
        
        try:
            eph = obj.ephemerides(closest_apparition=True)
            results = []
            for row in eph:
                results.append({
                    "targetname": row["targetname"],
                    "datetime_jd": float(row["datetime_jd"]),
                    "datetime_str": row["datetime_str"],
                    "RA": float(row["RA"]),
                    "DEC": float(row["DEC"]),
                    "RA_rate": float(row["RA_rate"]) if "RA_rate" in row.colnames else 0.0,
                    "DEC_rate": float(row["DEC_rate"]) if "DEC_rate" in row.colnames else 0.0,
                    "location": location,
                })
            return results
        except Exception as e:
            print(f"JPL Horizons query failed: {e}")
            return []

    def retrieve_data(self, record_id: str, **kwargs) -> Any:
        raise NotImplementedError("JPL Horizons does not provide data downloads.")
        
    def query_exact_epochs(self, target: str, epochs: List[float], **kwargs) -> List[Dict[str, Any]]:
        """
        Query JPL Horizons for ephemerides at exact JDs.
        """
        id_type = kwargs.get("id_type", "designation")
        location = kwargs.get("location", "I41")
        
        from astroquery.jplhorizons import Horizons
        
        # Horizons limits the number of discrete epochs in one query (often ~50-100)
        # For our scale, if it's small, we can pass them directly.
        obj = Horizons(id=target, id_type=id_type, location=location, epochs=epochs)
        
        try:
            eph = obj.ephemerides(closest_apparition=True)
            results = []
            for row in eph:
                results.append({
                    "targetname": row["targetname"],
                    "datetime_jd": float(row["datetime_jd"]),
                    "datetime_str": row["datetime_str"],
                    "RA": float(row["RA"]),
                    "DEC": float(row["DEC"]),
                    "RA_rate": float(row["RA_rate"]) if "RA_rate" in row.colnames else 0.0,
                    "DEC_rate": float(row["DEC_rate"]) if "DEC_rate" in row.colnames else 0.0,
                    "location": location,
                })
            return results
        except Exception as e:
            print(f"JPL Horizons exact epoch query failed: {e}")
            return []
