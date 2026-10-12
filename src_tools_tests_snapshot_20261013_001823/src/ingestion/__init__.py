from .registry import ConnectorRegistry
from .jpl_horizons import JPLHorizonsConnector
from .ztf import ZTFConnector
from .mast import MASTConnector
from .simbad import SIMBADConnector

# Instantiate and register available connectors
ConnectorRegistry.register(JPLHorizonsConnector())
ConnectorRegistry.register(ZTFConnector())
ConnectorRegistry.register(MASTConnector())
ConnectorRegistry.register(SIMBADConnector())
