"""Compatibility import; implementation lives in adapters.eip_client."""

import sys

from .adapters import eip_client as _implementation

sys.modules[__name__] = _implementation
