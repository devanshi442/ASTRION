from typing import Any, Dict, List
from .base import BaseIngestionConnector

class MASTConnector(BaseIngestionConnector):
    @property
    def source_id(self) -> str:
        return "mast_panstarrs"

    @property
    def capabilities(self) -> Dict[str, bool]:
        return {
            "ephemeris": False,
            "image": True,
            "catalog": True,
        }

    def query_metadata(self, **kwargs) -> List[Dict[str, Any]]:
        # TODO: Implement MAST Pan-STARRS query using astroquery.mast
        raise NotImplementedError("MAST connector is not yet safely implemented. Avoid fabricating queries.")

    def retrieve_data(self, record_id: str, **kwargs) -> Any:
        raise NotImplementedError()
