"""Optional native substrate backend discovery, independent of the target project."""

from __future__ import annotations

import importlib.metadata
import importlib.util

from ozagia.research.graph import ResearchError


def engine_preamble() -> None:
    if importlib.util.find_spec("substrate_rs") is None:
        raise ResearchError(
            "Native substrate runtime is unavailable; install a compatible substrate_rs backend."
        )


def provenance_stamp() -> dict:
    try:
        version = importlib.metadata.version("substrate-rs")
    except importlib.metadata.PackageNotFoundError:
        version = None
    return {
        "backend": "substrate_rs",
        "version": version,
        "source_provenance": "unverified_external_backend",
    }
