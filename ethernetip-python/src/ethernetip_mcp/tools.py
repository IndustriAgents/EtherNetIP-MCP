"""Compatibility imports; implementation lives in domain, application and protocol modules."""

import json as json
import math as math
import os as os
from collections.abc import Mapping as Mapping
from dataclasses import dataclass as dataclass
from pathlib import Path as Path
from typing import Annotated as Annotated
from typing import Any as Any

from pydantic import Field as Field
from pydantic import StrictInt as StrictInt

from ethernetip_mcp.application.resources import ToolResources as ToolResources
from ethernetip_mcp.domain.errors import EIPClientError as EIPClientError
from ethernetip_mcp.domain.errors import parse_bool as parse_bool
from ethernetip_mcp.domain.model import _INTEGER_TYPES as _INTEGER_TYPES
from ethernetip_mcp.domain.model import LOCAL_TOOLS as LOCAL_TOOLS
from ethernetip_mcp.domain.model import NOT_SENT as NOT_SENT
from ethernetip_mcp.domain.model import WRITE_TOOLS as WRITE_TOOLS
from ethernetip_mcp.domain.model import Alias as Alias
from ethernetip_mcp.domain.model import DataType as DataType
from ethernetip_mcp.domain.model import ElementCount as ElementCount
from ethernetip_mcp.domain.model import TagName as TagName
from ethernetip_mcp.domain.model import _error_meta as _error_meta
from ethernetip_mcp.domain.model import _round_for_type as _round_for_type
from ethernetip_mcp.domain.model import _scale as _scale
from ethernetip_mcp.domain.model import _scaling as _scaling
from ethernetip_mcp.domain.model import envelope as envelope
from ethernetip_mcp.domain.model import fail as fail
from ethernetip_mcp.domain.model import ok as ok
from ethernetip_mcp.eip_client import EIPClient as EIPClient
from ethernetip_mcp.infrastructure.maps import TagMap as TagMap
from ethernetip_mcp.infrastructure.tool_config import ToolConfig as ToolConfig

from .protocol.support import DEVICE_READ_ANNOTATIONS as DEVICE_READ_ANNOTATIONS
from .protocol.support import LOCAL_READ_ANNOTATIONS as LOCAL_READ_ANNOTATIONS
from .protocol.support import WRITE_ANNOTATIONS as WRITE_ANNOTATIONS
from .protocol.support import FastMCP as FastMCP
from .protocol.support import ToolAnnotations as ToolAnnotations
from .protocol.support import annotations_for as annotations_for
from .protocol.tools import register_tools as register_tools
