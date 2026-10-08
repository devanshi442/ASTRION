from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional
from datetime import datetime


class BaseIngestionConnector(ABC):
    """
    Base class for all astronomical data connectors.
    Enforces a standard interface for discovering capabilities, querying metadata,
    and handling small targeted retrievals.
    """
    
    @property
    @abstractmethod
    def source_id(self) -> str:
        """The canonical ID of the data source (e.g., 'irsa_ztf')."""
        pass

    @property
    @abstractmethod
    def capabilities(self) -> Dict[str, bool]:
        """Dictionary of capabilities (e.g., image, catalog, ephemeris)."""
        pass

    @abstractmethod
    def query_metadata(self, **kwargs) -> List[Dict[str, Any]]:
        """
        Query metadata (e.g., list of images covering an area, or an ephemeris).
        Must return normalized dictionary records.
        """
        pass

    @abstractmethod
    def retrieve_data(self, record_id: str, **kwargs) -> Any:
        """
        Retrieve a small targeted dataset (e.g., a FITS cutout).
        Do NOT download massive data.
        """
        pass
