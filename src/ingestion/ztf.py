import requests
from astropy.table import Table
import numpy as np
from typing import Any, Dict, List
from .base import BaseIngestionConnector

class ZTFConnector(BaseIngestionConnector):
    @property
    def source_id(self) -> str:
        return "irsa_ztf"

    @property
    def capabilities(self) -> Dict[str, bool]:
        return {
            "ephemeris": False,
            "image": True,
            "catalog": True,
        }

    def query_metadata(self, **kwargs) -> List[Dict[str, Any]]:
        """
        Query ZTF metadata from IRSA.
        Expects: ra (float), dec (float).
        """
        ra = kwargs.get("ra")
        dec = kwargs.get("dec")
        if ra is None or dec is None:
            raise ValueError("ra and dec are required.")

        url = "https://irsa.ipac.caltech.edu/ibe/search/ztf/products/sci"
        params = {"POS": f"{ra:.6f},{dec:.6f}", "INTERSECT": "CENTER"}
        
        try:
            r = requests.get(url, params=params, timeout=30)
            r.raise_for_status()
            
            # Use astropy Table to parse IPAC table format from memory
            lines = r.text.splitlines()
            # If the response is empty or just header
            if len(lines) < 4:
                return []
                
            tbl = Table.read(lines, format="ipac")
            results = []
            for row in tbl:
                # Basic validation
                if "obsjd" not in row.colnames:
                    continue
                ffd = str(int(row["filefracday"]))
                field = int(row["field"])
                ccdid = int(row["ccdid"])
                qid = int(row["qid"])
                filtercode = row["filtercode"]
                imgtypecode = row["imgtypecode"]
                
                # Construct data URL based on ZTF path rules
                original_uri = (f"https://irsa.ipac.caltech.edu/ibe/data/ztf/products/sci/"
                                f"{ffd[:4]}/{ffd[4:8]}/{ffd[8:]}/"
                                f"ztf_{ffd}_{field:06d}_{filtercode}_c{ccdid:02d}_"
                                f"{imgtypecode}_q{qid}_sciimg.fits")

                results.append({
                    "record_id": f"ztf_{ffd}_{field:06d}_c{ccdid:02d}_q{qid}",
                    "obsjd": float(row["obsjd"]),
                    "ra": float(row["ra"]) if "ra" in row.colnames else None,
                    "dec": float(row["dec"]) if "dec" in row.colnames else None,
                    "filtercode": filtercode,
                    "filefracday": ffd,
                    "field": field,
                    "ccdid": ccdid,
                    "imgtypecode": imgtypecode,
                    "qid": qid,
                    "original_uri": original_uri
                })
            return results
        except Exception as e:
            print(f"ZTF metadata query failed: {e}")
            return []

    def retrieve_data(self, record_id: str, **kwargs) -> Any:
        # Instead of just taking record_id, we expect the original_uri in kwargs
        original_uri = kwargs.get("original_uri")
        if not original_uri:
            raise ValueError("original_uri is required for ZTF FITS retrieval.")
        
        # We can also add cutout params if provided
        ra = kwargs.get("ra")
        dec = kwargs.get("dec")
        size = kwargs.get("size", 0.05)
        
        url = original_uri
        params = {}
        if ra is not None and dec is not None:
            params = {"center": f"{ra:.6f},{dec:.6f}", "size": str(size), "gzip": "false"}
            
        r = requests.get(url, params=params, timeout=60)
        return r
