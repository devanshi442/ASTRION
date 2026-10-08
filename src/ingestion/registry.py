from typing import Dict, Type
from .base import BaseIngestionConnector

class ConnectorRegistry:
    """Registry for all data ingestion connectors."""
    
    _connectors: Dict[str, BaseIngestionConnector] = {}

    @classmethod
    def register(cls, connector: BaseIngestionConnector):
        cls._connectors[connector.source_id] = connector

    @classmethod
    def get_connector(cls, source_id: str) -> BaseIngestionConnector:
        if source_id not in cls._connectors:
            raise ValueError(f"Unknown connector ID: {source_id}")
        return cls._connectors[source_id]

    @classmethod
    def get_all(cls) -> Dict[str, BaseIngestionConnector]:
        return cls._connectors
