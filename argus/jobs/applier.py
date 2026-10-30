"""Application results shared by the scheduler and the jobagent applier.

Applying itself is done by jobagent (``argus.jobs.jobagent_applier``); this module only holds the
result type and the markers the scheduler stores.
"""

from __future__ import annotations

from dataclasses import dataclass, field

APPLY_RESULTS = ("submitted", "needs_login", "needs_attestation", "needs_user", "skipped", "error")
# Marker in a queued posting's ``status_detail``: an earlier run may already have submitted it.
PRIOR_ATTEMPT = "prior attempt"


@dataclass(frozen=True)
class ApplyResult:
    result: str  # one of APPLY_RESULTS
    detail: str
    follow_up: str | None = None
    guessed_answers: list[str] = field(default_factory=list)
    cost_usd: float | None = None
    raw: str = ""
    accounts_created: tuple[str, ...] = ()  # sites jobagent registered an account on
    company: str | None = None
    role: str | None = None
