"""Offline analyzers used by deep review."""

from .static import SUBJECTS, analyze as analyze_static
from .docs_api import analyze as analyze_docs_api
from .data import analyze as analyze_data

__all__ = ["SUBJECTS", "analyze_static", "analyze_docs_api", "analyze_data"]
