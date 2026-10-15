"""Compose the protocol use cases over one instance-owned resource set."""

from .common import CommonOperations
from .diagnostics import DiagnosticsOperations
from .metadata import MetadataOperations
from .read import ReadOperations
from .write import WriteOperations


class Operations(CommonOperations, ReadOperations, WriteOperations, DiagnosticsOperations, MetadataOperations):
    pass
