"""Tool policy for the jobs agents: which tools and files the scout and the applier may use.

``make_policy`` returns a ``GuardPolicy``: an async ``can_use_tool`` callback for
``ClaudeAgentOptions`` that also exposes the pure ``decide(tool_name, input)`` core and a
``PreToolUse`` hook (``pre_tool_use_hook``) that denies the same calls. The hook gates every
tool call even where the CLI would auto-approve without asking (``can_use_tool`` is only
consulted for calls that would prompt), so the builders install both.

Paths are resolved with ``Path.resolve()`` (``..`` and symlinks followed) relative to the
agent's cwd, the internships directory. Name checks are case-insensitive because macOS
file systems are. Malformed input (NUL bytes, wrong types) is denied, never raised.

Scout: Read text files under about_me (not SENSITIVE*, documents/, binaries); Write/Edit only
``INTERNSHIPS.md``, ``seen_companies.txt`` and files under ``raw/`` in the internships dir.

Apply: Read only the brief and profile files (``APPLY_READABLE``), the résumé and transcript
PDFs (``*transcript*.pdf`` directly in about_me); upload only the résumé and those PDFs;
Write/Edit only under ``internships/applications/``; Chrome tools from ``CHROME_ALLOWED`` only.
``navigate`` may only go to the posting's site or a known ATS (``ATS_DOMAINS``), and
``javascript_tool`` only runs in a tab whose last ``navigate`` (tracked per tabId from tool
inputs) went to such a host. The policy cannot see where clicks lead, so this is a best-effort
fence around the user's logged-in browser, not a sandbox.
"""

from __future__ import annotations

import fnmatch
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit

from claude_agent_sdk import PermissionResultAllow, PermissionResultDeny

Mode = Literal["scout", "apply"]
MODES = ("scout", "apply")

CHROME_PREFIX = "mcp__claude-in-chrome__"
CHROME_ALLOWED = {
    "tabs_context_mcp", "tabs_create_mcp", "tabs_close_mcp", "navigate", "computer", "find",
    "read_page", "get_page_text", "form_input", "file_upload", "browser_batch",
    "list_connected_browsers",
}
CHROME_UPLOAD = "file_upload"
CHROME_NAVIGATE = "navigate"
CHROME_JS = "javascript_tool"
CHROME_BATCH = "browser_batch"

# Applicant tracking systems the applier may navigate to (and run JS on), with their subdomains.
ATS_DOMAINS = (
    "myworkdayjobs.com", "myworkdaysite.com", "greenhouse.io", "lever.co", "ashbyhq.com", "icims.com",
    "smartrecruiters.com", "avature.net", "successfactors.com", "taleo.net", "oraclecloud.com",
    "jobvite.com", "workable.com",
)
# The scout's ``ats`` label -> the host family it implies.
ATS_FAMILIES = {
    "workday": ("myworkdayjobs.com", "myworkdaysite.com"),
    "greenhouse": ("greenhouse.io",),
    "lever": ("lever.co",),
    "ashby": ("ashbyhq.com",),
    "icims": ("icims.com",),
    "smartrecruiters": ("smartrecruiters.com",),
    "avature": ("avature.net",),
    "successfactors": ("successfactors.com",),
    "taleo": ("taleo.net",),
    "oracle": ("oraclecloud.com",),
    "jobvite": ("jobvite.com",),
    "workable": ("workable.com",),
}
# Second-level labels under which the registrable domain has three labels (example.co.uk).
_SHORT_SLDS = {"co", "com", "ac", "gov", "org", "net", "edu", "gc", "qc", "on"}

APPLY_READABLE_INTERNSHIPS = {"apply_brief.md"}
APPLY_READABLE_ABOUT_ME = {"profile.md", "education.md", "work_and_projects.md", "open_questions.md"}
TRANSCRIPT_GLOB = "*transcript*.pdf"
SCOUT_WRITABLE = {"internships.md", "seen_companies.txt"}
SCOUT_WRITABLE_DIR = "raw"

SENSITIVE_MARKER = "sensitive"  # any path component starting with this (any case) is off limits
DOCUMENTS_DIR = "documents"  # scans of IDs, passports, etc. directly under about_me
BINARY_SUFFIXES = {
    ".pdf", ".png", ".jpg", ".jpeg", ".gif", ".heic", ".heif", ".webp", ".tif", ".tiff", ".bmp",
    ".zip", ".gz", ".tar", ".7z", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx", ".pages",
    ".key", ".numbers", ".mov", ".mp4", ".m4a", ".mp3", ".db", ".sqlite",
}
WRITE_TOOLS = {"Write", "Edit", "MultiEdit"}
SCOUT_ONLY = {"WebSearch", "WebFetch"}


def _within(path: Path, root: Path) -> bool:
    return path == root or path.is_relative_to(root)


def url_host(url: object) -> str | None:
    """Lower-case host of an http(s) URL (scheme optional, defaulting to https), else None."""
    if not isinstance(url, str) or not url.strip() or "\x00" in url or any(c.isspace() for c in url.strip()):
        return None
    url = url.strip()
    if "://" not in url:
        if ":" in url.split("/", 1)[0] and not url.split("/", 1)[0].rsplit(":", 1)[-1].isdigit():
            return None  # javascript:, data:, mailto: ...
        url = "https://" + url
    try:
        parts = urlsplit(url)
        host = parts.hostname
    except ValueError:
        return None
    if parts.scheme.lower() not in ("http", "https") or not host:
        return None
    return host.rstrip(".").lower() or None


def registrable_domain(host: str) -> str:
    """Approximate registrable domain (no public-suffix list): ``a.b.example.com`` -> ``example.com``."""
    labels = host.split(".")
    if len(labels) >= 3 and len(labels[-1]) == 2 and labels[-2] in _SHORT_SLDS:
        return ".".join(labels[-3:])
    return ".".join(labels[-2:])


def _host_in(host: str, domains) -> bool:
    return any(host == d or host.endswith("." + d) for d in domains)


def is_ats_host(host: str | None) -> bool:
    return bool(host) and _host_in(host, ATS_DOMAINS)


def auto_apply_eligible(posting: dict) -> bool:
    """True if the posting's URL is on a known ATS (or on the host family its ``ats`` names)."""
    host = url_host(posting.get("url"))
    if host is None:
        return False
    if is_ats_host(host):
        return True
    family = ATS_FAMILIES.get(str(posting.get("ats") or "").strip().casefold())
    return bool(family) and _host_in(host, family)


@dataclass(frozen=True)
class GuardPolicy:
    about_me_dir: Path
    internships_dir: Path
    resume_path: Path
    mode: Mode
    posting_url: str | None = None
    # tabId -> host of the last allowed ``navigate`` in that tab (None: unknown). Mutable state
    # inside a frozen dataclass: the policy instance belongs to one agent run.
    tab_hosts: dict = field(default_factory=dict, compare=False, repr=False)

    # --- path helpers ---

    def _resolve(self, raw: object) -> Path | None:
        if not isinstance(raw, str) or not raw.strip() or "\x00" in raw:
            return None
        try:
            p = Path(raw.strip()).expanduser()
            if not p.is_absolute():
                p = self.internships_dir / p
            return p.resolve()
        except (OSError, RuntimeError, ValueError):  # symlink loops, bad ~user, etc.
            return None

    def _blocked(self, raw: object, path: Path) -> str | None:
        """Reason ``path`` (resolved) is off limits for any access, else None."""
        names = list(path.parts)
        if isinstance(raw, str):
            names += list(Path(raw).parts)
        if any(n.casefold().startswith(SENSITIVE_MARKER) for n in names):
            return "SENSITIVE_IDS / sensitive files are never accessible"
        about = self.about_me_dir
        if _within(path, about) and path != about:
            first = path.relative_to(about).parts[0]
            if first.casefold() == DOCUMENTS_DIR:
                return "about_me/documents/ is never accessible"
        return None

    def _readable(self, raw: object) -> tuple[bool, str]:
        path = self._resolve(raw)
        if path is None:
            return False, "a non-empty file path is required"
        reason = self._blocked(raw, path)
        if reason:
            return False, reason
        if path == self.resume_path:
            return True, "the résumé is readable"
        if self.mode == "apply":
            if self._is_transcript(path):
                return True, "transcript PDF"
            name = path.name.casefold()
            if path.parent == self.internships_dir and name in APPLY_READABLE_INTERNSHIPS:
                return True, "the apply brief"
            if path.parent == self.about_me_dir and name in APPLY_READABLE_ABOUT_ME:
                return True, "profile file"
            return False, ("the applier may only read APPLY_BRIEF.md, PROFILE.md, EDUCATION.md, "
                           "WORK_AND_PROJECTS.md, OPEN_QUESTIONS.md, the résumé and transcript PDFs")
        if not _within(path, self.about_me_dir):
            return False, f"only files under {self.about_me_dir} may be read"
        if path.suffix.casefold() in BINARY_SUFFIXES:
            return False, "binary, image and PDF files may not be read"
        return True, "text file under about_me"

    def _is_transcript(self, path: Path) -> bool:
        """A ``*transcript*.pdf`` directly in about_me (resolved path: symlinks already followed)."""
        return path.parent == self.about_me_dir and fnmatch.fnmatch(path.name.casefold(), TRANSCRIPT_GLOB)

    def _tree_escapes(self, root: Path) -> bool:
        """True if a directory tree contains a symlink pointing at a blocked or outside file."""
        for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
            for name in (*dirnames, *filenames):
                entry = Path(dirpath) / name
                if entry.is_symlink():
                    try:
                        target = entry.resolve()
                    except (OSError, RuntimeError):
                        return True
                    if self._blocked(None, target) or not _within(target, self.internships_dir):
                        return True
        return False

    # --- per-tool rules ---

    def _read(self, input: dict) -> tuple[bool, str]:
        return self._readable(input.get("file_path"))

    def _write(self, input: dict) -> tuple[bool, str]:
        raw = input.get("file_path")
        path = self._resolve(raw)
        if path is None:
            return False, "a non-empty file path is required"
        reason = self._blocked(raw, path)
        if reason:
            return False, reason
        if self.mode == "apply":
            root = self.internships_dir / "applications"
            if path == root or not path.is_relative_to(root):
                return False, f"apply may only write files under {root}"
            return True, f"write under {root}"
        if path.is_relative_to(self.internships_dir) and path != self.internships_dir:
            rel = [p.casefold() for p in path.relative_to(self.internships_dir).parts]
            if len(rel) == 1 and rel[0] in SCOUT_WRITABLE:
                return True, "tracker file"
            if len(rel) >= 2 and rel[0] == SCOUT_WRITABLE_DIR:
                return True, "raw notes"
        return False, "the scout may only write INTERNSHIPS.md, seen_companies.txt and files under raw/"

    def _glob(self, input: dict) -> tuple[bool, str]:
        pattern = input.get("pattern")
        if not isinstance(pattern, str) or not pattern:
            return False, "a glob pattern is required"
        if ".." in Path(pattern).parts or Path(pattern).expanduser().is_absolute() or pattern.startswith("~"):
            return False, "glob patterns must be relative and must not contain '..'"
        raw = input.get("path") or "."
        path = self._resolve(raw)
        if path is None:
            return False, "invalid path"
        reason = self._blocked(raw, path)
        if reason:
            return False, reason
        if not _within(path, self.about_me_dir):
            return False, f"Glob is limited to {self.about_me_dir}"
        return True, "glob under about_me"

    def _grep(self, input: dict) -> tuple[bool, str]:
        raw = input.get("path") or "."
        path = self._resolve(raw)
        if path is None:
            return False, "invalid path"
        reason = self._blocked(raw, path)
        if reason:
            return False, reason
        if path.is_dir():
            # A recursive search of about_me itself would scan SENSITIVE_IDS.md.
            if not _within(path, self.internships_dir):
                return False, f"Grep over a directory is limited to {self.internships_dir}"
            if self._tree_escapes(path):
                return False, "directory contains a symlink leading outside internships/"
            return True, "grep inside internships"
        return self._readable(raw)

    def _upload(self, input: dict) -> tuple[bool, str]:
        paths = input.get("paths")
        if not isinstance(paths, list) or not paths:
            return False, "file_upload needs a non-empty list of paths"
        for raw in paths:
            path = self._resolve(raw)
            if path is None:
                return False, f"invalid upload path {raw!r}"
            reason = self._blocked(raw, path)
            if reason:
                return False, reason
            if path == self.resume_path or self._is_transcript(path):
                continue
            return False, f"only the résumé or *transcript*.pdf files in about_me may be uploaded, not {raw}"
        return True, "upload of résumé / transcript PDFs"

    # --- Chrome (apply mode) ---

    def _nav_allowed(self, host: str | None) -> bool:
        if not host:
            return False
        if is_ats_host(host):
            return True
        posting = url_host(self.posting_url)
        if posting is None:
            return False
        return host == posting or _host_in(host, (registrable_domain(posting),))

    def _chrome(self, short: str, input: dict, tabs: dict) -> tuple[bool, str]:
        """Decide one Chrome call; ``tabs`` (tabId -> host) is updated for allowed navigations."""
        if short == CHROME_UPLOAD:
            return self._upload(input)
        if short == CHROME_NAVIGATE:
            url = input.get("url")
            tab = input.get("tabId")
            if isinstance(url, str) and url.strip().casefold() in ("back", "forward"):
                tabs[tab] = None  # destination unknown: no JavaScript there until the next navigate
                return True, "history navigation"
            host = url_host(url)
            if not self._nav_allowed(host):
                return False, f"navigate only to the posting's site or a known ATS, not {url!r}"
            tabs[tab] = host
            return True, f"navigate to {host}"
        if short == CHROME_JS:
            tab = input.get("tabId")
            host = tabs.get(tab) if isinstance(tab, (int, float)) and not isinstance(tab, bool) else None
            if not self._nav_allowed(host):
                return False, ("javascript_tool only runs in a tab you navigated (navigate with this "
                               "tabId) to the posting's site or a known ATS")
            return True, f"JavaScript on {host}"
        if short == CHROME_BATCH:
            actions = input.get("actions")
            if not isinstance(actions, list) or not actions:
                return False, "browser_batch needs a non-empty 'actions' list"
            for item in actions:
                if not isinstance(item, dict) or not isinstance(item.get("name"), str) \
                        or not isinstance(item.get("input"), dict):
                    return False, "browser_batch items must be {name, input}"
                inner = item["name"].removeprefix(CHROME_PREFIX)
                if inner == CHROME_BATCH:
                    return False, "browser_batch cannot be nested"
                if inner not in CHROME_ALLOWED and inner != CHROME_JS:
                    return False, f"browser_batch item {inner} is not allowed"
                allowed, reason = self._chrome(inner, item["input"], tabs)
                if not allowed:
                    return False, f"browser_batch item {inner}: {reason}"
            return True, "browser_batch of allowed actions"
        if short in CHROME_ALLOWED:
            return True, "Chrome tool"
        return False, f"{short} is not allowed for the applier"

    def decide(self, tool_name: str, input: dict[str, Any]) -> tuple[bool, str]:
        """``(allowed, reason)`` for one tool call; never raises (errors deny). Pure except that
        allowed Chrome navigations update ``tab_hosts``."""
        try:
            return self._decide(tool_name, input)
        except Exception as e:  # noqa: BLE001 - a policy bug must deny, not crash the run
            return False, f"invalid tool input ({type(e).__name__})"

    def _decide(self, tool_name: str, input: dict[str, Any]) -> tuple[bool, str]:
        if not isinstance(tool_name, str):
            return False, "invalid tool name"
        if not isinstance(input, dict):
            input = {}
        if tool_name == "ToolSearch":
            return True, "ToolSearch is always allowed"
        if tool_name == "Read":
            return self._read(input)
        if tool_name in WRITE_TOOLS:
            return self._write(input)
        if tool_name in SCOUT_ONLY:
            return (True, "web access for the scout") if self.mode == "scout" else (
                False, f"{tool_name} is only available to the scout")
        if tool_name in ("Glob", "Grep"):
            if self.mode != "scout":
                return False, f"{tool_name} is only available to the scout"
            return self._glob(input) if tool_name == "Glob" else self._grep(input)
        if tool_name.startswith(CHROME_PREFIX):
            if self.mode != "apply":
                return False, "Chrome tools are only available to the applier"
            short = tool_name[len(CHROME_PREFIX):]
            tabs = dict(self.tab_hosts)
            allowed, reason = self._chrome(short, input, tabs)
            if allowed:  # commit navigations only when the whole call (or batch) is allowed
                self.tab_hosts.clear()
                self.tab_hosts.update(tabs)
            return allowed, reason
        return False, f"{tool_name} is not available to the jobs {self.mode} agent"

    # --- SDK adapters ---

    async def __call__(self, tool_name: str, input: dict[str, Any], context: Any):
        allowed, reason = self.decide(tool_name, input)
        if allowed:
            return PermissionResultAllow()
        return PermissionResultDeny(message=f"Denied by Argus policy: {reason}")

    async def pre_tool_use_hook(self, input: dict[str, Any], tool_use_id: str | None, context: Any) -> dict:
        """PreToolUse hook: deny what ``decide`` denies; otherwise defer to normal permissions."""
        allowed, reason = self.decide(input.get("tool_name", ""), input.get("tool_input") or {})
        if allowed:
            return {}
        return {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": f"Denied by Argus policy: {reason}",
            }
        }


def make_policy(
    about_me_dir: Path, internships_dir: Path, resume_path: Path, mode: Mode, posting_url: str | None = None,
) -> GuardPolicy:
    """Build the tool policy; directories and the résumé path are resolved once here.
    ``posting_url`` (apply mode) is the one site besides known ATSs the applier may navigate to."""
    if mode not in MODES:
        raise ValueError(f"mode must be 'scout' or 'apply', got {mode!r}")
    return GuardPolicy(
        about_me_dir=Path(about_me_dir).expanduser().resolve(),
        internships_dir=Path(internships_dir).expanduser().resolve(),
        resume_path=Path(resume_path).expanduser().resolve(),
        mode=mode,
        posting_url=posting_url,
    )
