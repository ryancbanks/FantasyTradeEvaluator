"""Public data-readiness API.

Implementation is split by responsibility behind this stable import surface.
"""

from ._data_readiness_model import DataReadinessSnapshot
from ._data_readiness_report import build_bundle_data_readiness
from ._data_readiness_snapshot import build_data_readiness_snapshot

# Preserve the historical public class identity for repr and pickle consumers.
DataReadinessSnapshot.__module__ = __name__

__all__ = (
    "DataReadinessSnapshot",
    "build_bundle_data_readiness",
    "build_data_readiness_snapshot",
)
