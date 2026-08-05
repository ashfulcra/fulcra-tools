"""fulcra-purpleair — a Fulcra Collect plugin that ingests PurpleAir
air-quality sensor readings (cloud API or LAN) as per-measure Fulcra tracks.
"""
from .client import parse_api_response, parse_local_response
from .definitions import METRICS, MetricDef
from .ingest import build_records
from .models import Reading, pm25_to_aqi

__all__ = [
    "Reading",
    "pm25_to_aqi",
    "parse_api_response",
    "parse_local_response",
    "METRICS",
    "MetricDef",
    "build_records",
]
