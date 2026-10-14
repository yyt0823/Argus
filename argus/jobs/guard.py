"""Tool policy for the jobs agents: which tools and files the scout and the applier may use.

``make_policy`` returns a ``GuardPolicy``: an async ``can_use_tool`` callback for
``ClaudeAgentOptions`` that also exposes the pure ``decide(tool_name, input)`` core and a
``PreToolUse`` hook (``pre_tool_use_hook``) that denies the same calls. The hook gates every
tool call even where the CLI would auto-approve without asking (``can_use_tool`` is only
consulted for calls that would prompt), so the builders install both.

Paths are resolved with ``Path.resolve()`` (``..`` and symlinks followed) relative to the
agent's cwd, the internships directory. Name checks are case-insensitive because macOS
file systems are.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from claude_agent_sdk import PermissionResultAllow, PermissionResultDeny

Mode = Literal["scout", "apply"]
MODES = ("scout", "apply")

CHROME_PREFIX = "mcp__claude-in-chrome__"
CHROME_FORBIDDEN = {"select_browser", "switch_browser", "upload_image"}
CHROME_UPLOAD = "file_upload"

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


@dataclass(frozen=True)
class GuardPolicy:
    about_me_dir: Path
    internships_dir: Path
    resume_path: Path
    mode: Mode

    # --- path helpers ---

    def _resolve(self, raw: object) -> Path | None:
        if not isinstance(raw, str) or not raw.strip():
            return None
        p = Path(raw.strip()).expanduser()
        if not p.is_absolute():
            p = self.internships_dir / p
        try:
            return p.resolve()
        except (OSError, RuntimeError):  # symlink loops etc.
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
        if not _within(path, self.about_me_dir):
            return False, f"only files under {self.about_me_dir} may be read"
        if path.suffix.casefold() in BINARY_SUFFIXES:
            return False, "binary, image and PDF files may not be read"
        return True, "text file under about_me"

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
        root = self.internships_dir / "applications" if self.mode == "apply" else self.internships_dir
        if path == root or not path.is_relative_to(root):
            return False, f"{self.mode} may only write files under {root}"
        return True, f"write under {root}"

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
            if path == self.resume_path:
                continue
            if not (_within(path, self.about_me_dir) and path.suffix.casefold() == ".pdf"):
                return False, f"only the résumé or PDFs in about_me (transcripts) may be uploaded, not {raw}"
        return True, "upload of résumé / transcript PDFs"

    def decide(self, tool_name: str, input: dict[str, Any]) -> tuple[bool, str]:
        """Pure decision: ``(allowed, reason)`` for one tool call."""
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
            if short in CHROME_FORBIDDEN:
                return False, f"{short} is not allowed (never pick or switch browsers; no screenshot uploads)"
            if short == CHROME_UPLOAD:
                return self._upload(input)
            return True, "Chrome tool"
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


def make_policy(about_me_dir: Path, internships_dir: Path, resume_path: Path, mode: Mode) -> GuardPolicy:
    """Build the tool policy; directories and the résumé path are resolved once here."""
    if mode not in MODES:
        raise ValueError(f"mode must be 'scout' or 'apply', got {mode!r}")
    return GuardPolicy(
        about_me_dir=Path(about_me_dir).expanduser().resolve(),
        internships_dir=Path(internships_dir).expanduser().resolve(),
        resume_path=Path(resume_path).expanduser().resolve(),
        mode=mode,
    )
