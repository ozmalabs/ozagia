"""Pure mechanical checks for research workflow records.

These checks report workflow completeness and provenance caveats. They do not
validate scientific conclusions or certify that a recorded run happened.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Mapping


CONFIRMATORY_FIELDS = (
    "hypothesis",
    "readout",
    "sample_size_justification",
    "independent_unit",
    "seed_allocation",
    "controls",
    "decision_rule",
    "fixed_analysis",
)


def _present(value: Any) -> bool:
    return value is not None and value != "" and value != [] and value != {}


def _time(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        # Treat a timezone-free timestamp as UTC so comparisons against
        # explicit UTC offsets are well-defined and deterministic.
        if parsed.utcoffset() is None:
            from datetime import timezone

            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed
    except ValueError:
        return None


def check_research_workflow(
    investigation: Mapping[str, Any],
    protocol: Mapping[str, Any] | None = None,
    run: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Return a deterministic checklist for one investigation/protocol/run.

    ``mode`` is one of exploratory, confirmatory, deductive, qualitative, or
    empirical. Unknown values are reported as a blocker rather than guessed.
    Protocol records may carry ``immutable``, ``revision``, ``recorded_at``;
    runs may carry ``protocol_revision``, ``started_at``, and receipt fields.
    """
    mode = investigation.get("mode", "unknown")
    blockers: list[str] = []
    notices: list[str] = []
    required: list[str] = []
    label = (
        mode
        if mode in {"exploratory", "confirmatory", "deductive", "qualitative", "empirical"}
        else "unknown"
    )
    if label == "unknown":
        blockers.append("investigation mode is missing or unrecognized")
    if label == "exploratory":
        notices.append("label all results exploratory; they do not count as confirmatory")
    if label == "confirmatory":
        required = list(CONFIRMATORY_FIELDS)
        if protocol is None:
            blockers.append("confirmatory work requires a prospective protocol")
        else:
            missing = [k for k in CONFIRMATORY_FIELDS if not _present(protocol.get(k))]
            blockers.extend(f"protocol missing {key}" for key in missing)
            if protocol.get("two_sided") is not True:
                blockers.append("protocol must state a two-sided hypothesis")
            if not _present(protocol.get("revision")) or protocol.get("immutable") is not True:
                blockers.append("protocol needs an immutable revision reference")
            prereg_time = _time(protocol.get("recorded_at"))
            run_time = _time((run or {}).get("started_at"))
            if run is not None:
                if prereg_time is None or run_time is None:
                    blockers.append(
                        "valid protocol recorded_at and run started_at timestamps are required"
                    )
                elif prereg_time >= run_time:
                    blockers.append(
                        "protocol was not recorded before the run; run cannot be confirmatory"
                    )
                if protocol.get("revision") != run.get("protocol_revision"):
                    blockers.append("run does not reference the preregistered protocol revision")
    elif label in {"deductive", "qualitative"}:
        reason = investigation.get("empirical_exemption_reason")
        if not _present(reason):
            blockers.append("empirical exemption reason is required for RNG and power fields")
        else:
            notices.append(f"empirical RNG/power exemption recorded: {reason}")
    elif label == "empirical":
        notices.append(
            "classify as empirical; choose exploratory or confirmatory before interpreting"
        )

    if run is not None:
        if run.get("receipt_status") == "manual":
            notices.append("receipt is manually asserted, not captured execution")
            blockers.append("run receipt is unavailable or unverified")
        elif run.get("receipt_status") != "captured":
            blockers.append("run receipt is unavailable or unverified")
        if run.get("exit_status") not in (None, 0):
            notices.append(
                f"run failed with exit status {run.get('exit_status')}; preserve as a failed run"
            )
        if run.get("engine_status") in {"stale", "diverged", "unavailable"}:
            blockers.append(f"engine provenance is {run.get('engine_status')}")
        if label == "confirmatory" and not _present(run.get("controls")):
            blockers.append("run receipt does not record control outcomes")
    return {
        "label": label,
        "confirmatory_eligible": label == "confirmatory" and not blockers,
        "required_protocol_fields": required,
        "blockers": blockers,
        "notices": notices,
    }


def check_finite_math(
    *,
    domain: Any,
    bounds: Any,
    oracle_independent: bool | None,
    verification_label: str,
    oracle_description: str | None = None,
) -> dict[str, Any]:
    """Check metadata needed to report a finite mathematical computation honestly."""
    blockers: list[str] = []
    notices: list[str] = []
    if not _present(domain):
        blockers.append("finite check must state its domain")
    if not _present(bounds):
        blockers.append("finite check must state its bounds")
    if oracle_independent is None:
        blockers.append("oracle independence is unknown; state and justify it")
    elif oracle_independent and not _present(oracle_description):
        blockers.append("describe the independent oracle")
    elif not oracle_independent:
        notices.append("oracle is not independent; report this limitation")
    if verification_label not in {"finite_verification", "proof"}:
        blockers.append("verification_label must be finite_verification or proof")
    if verification_label == "finite_verification":
        notices.append("finite verification is not a universal proof")
    elif verification_label == "proof":
        notices.append("proof label is recorded but this checker does not verify proofs")
    return {
        "complete": not blockers,
        "verification_label": verification_label,
        "blockers": blockers,
        "notices": notices,
    }
