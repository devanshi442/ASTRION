from typing import Any, Dict, List
from .base import BaseIngestionConnector

class SIMBADConnector(BaseIngestionConnector):
    @property
    def source_id(self) -> str:
        return "simbad"

    @property
    def capabilities(self) -> Dict[str, bool]:
        return {
            "ephemeris": False,
            "image": False,
            "catalog": True,
        }

    def query_metadata(self, **kwargs) -> List[Dict[str, Any]]:
        # TODO: Implement SIMBAD query using astroquery.simbad
        raise NotImplementedError("SIMBAD connector is not yet safely implemented. Avoid fabricating queries.")

    def retrieve_data(self, record_id: str, **kwargs) -> Any:
        raise NotImplementedError()
