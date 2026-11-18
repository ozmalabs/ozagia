"""Offline application profiling and telemetry, joined to the graph.

Read existing profiler or telemetry artifacts, never start a profiler or
import the target project.  See :mod:`ozagia.core.profile.ingest` for the
supported inputs and :mod:`ozagia.core.profile.report` for the report shape.
"""
from __future__ import annotations

from .analyze import SORTS, analyze, compare_analyses
from .collect import (
    TOOLS, CollectionError, CollectionRefused, build_command, run_collection,
)
from .graph import GraphIndex
from .ingest import FORMATS, WEIGHT_UNITS, detect_format, ingest_file
from .model import (
    FormatError, ProfileBuilder, ProfileError, REPORT_NAMESPACE,
    SCHEMA_NAMESPACE, VERSION, profile_hash, validate_profile,
)
from .report import (
    build_report, build_report_from_profile, compare_reports, format_report,
    report_hash, report_json, write_report,
)

__all__ = [
    "FORMATS", "FormatError", "GraphIndex", "ProfileBuilder", "ProfileError",
    "REPORT_NAMESPACE", "SCHEMA_NAMESPACE", "SORTS", "TOOLS", "VERSION",
    "WEIGHT_UNITS", "CollectionError", "CollectionRefused", "analyze",
    "build_command", "build_report", "build_report_from_profile",
    "compare_analyses", "compare_reports", "detect_format", "format_report",
    "ingest_file", "profile_hash", "report_hash", "report_json",
    "run_collection", "validate_profile", "write_report",
]
