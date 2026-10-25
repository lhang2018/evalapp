"""外部事实核验与可信交付（ALN-28-A）。"""

from .claims import Claim, ClaimState
from .crosscheck import FactCrossChecker, Verdict
from .delivery import DeliveryError, TrustedDelivery
from .sources import (SourceFetchError, SourceRecord, fetch_source,
                      fixture_source, load_sources)

__all__ = ["Claim", "ClaimState", "FactCrossChecker", "Verdict",
           "DeliveryError", "TrustedDelivery", "SourceFetchError",
           "SourceRecord", "fetch_source", "fixture_source", "load_sources"]
