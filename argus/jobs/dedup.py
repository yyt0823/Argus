"""Duplicate check: never apply twice to the same job.

``check_duplicate`` is a pure function over the applications tracker, earlier
Argus submissions and jobagent's own log. The normalisation helpers are public
because later tasks reuse them.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal
from urllib.parse import parse_qsl, urlencode, urlsplit

_COMPANY_STOPWORDS = {"inc", "ltd", "corp", "canada", "group", "technologies"}
_ROLE_STOPWORDS = {
    "intern", "internship", "co", "op", "coop", "2026", "2027",
    "winter", "summer", "fall", "new", "grad",
}
_KEEP_QUERY = {"company", "jobid", "gh_jid"}
_JACCARD_THRESHOLD = 0.6
_COUNTED_JOBAGENT = {"submitted", "already_applied"}

_REQ_RE = re.compile(
    r"\bJR\d+"
    r"|\bR\d{5,}"
    r"|\bREQ[-_ ]?\d+"
    r"|\((\d{5,7})\)"
    r"|_(\d{5,7})(?=$|[/?#])",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class DupVerdict:
    kind: Literal["none", "duplicate", "possible"]
    detail: str = ""


def norm_company(s: str | None) -> str:
    s = re.sub(r"\([^)]*\)", " ", (s or "").casefold())
    words = re.sub(r"[^\w\s]", " ", s).split()
    return " ".join(w for w in words if w not in _COMPANY_STOPWORDS)


def req_id(text: str | None) -> str | None:
    """First requisition id found in any text (URL or role), or None."""
    m = _REQ_RE.search(text or "")
    if not m:
        return None
    return m.group(1) or m.group(2) or m.group(0)


def norm_url(url: str | None) -> str:
    if not url or not url.strip():
        return ""
    parts = urlsplit(url.strip())
    query = urlencode(
        [(k, v) for k, v in parse_qsl(parts.query) if k.casefold() in _KEEP_QUERY]
    )
    out = parts.netloc.lower() + parts.path.rstrip("/")
    return out + ("?" + query if query else "")


def role_tokens(role: str | None) -> set[str]:
    words = re.findall(r"\w+", (role or "").casefold())
    return {w for w in words if len(w) >= 2 and w not in _ROLE_STOPWORDS}


def jaccard(a: set[str], b: set[str]) -> float:
    union = a | b
    return len(a & b) / len(union) if union else 0.0


def _norm_role(role: str | None) -> str:
    return " ".join(re.findall(r"\w+", (role or "").casefold()))


def _req_key(*texts: str | None) -> str | None:
    for t in texts:
        r = req_id(t)
        if r:
            return r.casefold()
    return None


def _compare(posting: dict, cand: dict) -> str:
    """Return 'duplicate', 'possible' or 'none' for one candidate."""
    p_url, c_url = norm_url(posting.get("url")), norm_url(cand["url"])
    if p_url and p_url == c_url:
        return "duplicate"
    p_co = norm_company(posting.get("company"))
    if not p_co or p_co != norm_company(cand["company"]):
        return "none"
    p_req = _req_key(posting.get("url"), posting.get("role"))
    c_req = cand["req"] or _req_key(cand["url"], cand["role"])
    if p_req and c_req:
        return "duplicate" if p_req == c_req else "none"
    p_role = posting.get("role")
    if jaccard(role_tokens(p_role), role_tokens(cand["role"])) >= _JACCARD_THRESHOLD:
        return "possible"
    if _norm_role(p_role) and _norm_role(p_role) == _norm_role(cand["role"]):
        return "duplicate"
    return "none"


def _candidates(tracker_rows, submitted_postings, jobagent_entries):
    for r in tracker_rows:
        yield {
            "url": r.get("url"), "company": r.get("company"), "role": r.get("role"),
            "req": _req_key(r.get("req_id")),
            "label": f"tracker #{r.get('id')}",
        }
    for p in submitted_postings:
        yield {
            "url": p.get("url"), "company": p.get("company"), "role": p.get("role"),
            "req": None, "label": f"posting #{p.get('id')}",
        }
    for e in jobagent_entries:
        if e.get("status") not in _COUNTED_JOBAGENT:
            continue
        yield {
            "url": e.get("job_url"), "company": e.get("company"),
            "role": e.get("job_title"), "req": None,
            "label": f"jobagent {str(e.get('at') or '')[:10]}",
        }


def check_duplicate(
    posting: dict,
    tracker_rows: list[dict],
    submitted_postings: list[dict],
    jobagent_entries: list[dict],
) -> DupVerdict:
    possible: DupVerdict | None = None
    for cand in _candidates(tracker_rows, submitted_postings, jobagent_entries):
        kind = _compare(posting, cand)
        if kind == "none":
            continue
        detail = f"{cand['label']} {cand['company'] or ''} — {cand['role'] or ''}"
        if kind == "duplicate":
            return DupVerdict("duplicate", detail)
        possible = possible or DupVerdict("possible", detail)
    return possible or DupVerdict("none", "")
