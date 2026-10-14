"""Allow/deny matrix for the jobs agents' tool policy, on a synthetic about_me tree."""

import pytest
from claude_agent_sdk import PermissionResultAllow, PermissionResultDeny, ToolPermissionContext

from argus.jobs.guard import make_policy


@pytest.fixture
def tree(tmp_path):
    about = tmp_path / "about_me"
    intern = about / "internships"
    (intern / "applications").mkdir(parents=True)
    (intern / "raw").mkdir()
    (about / "documents").mkdir()
    for name in ("PROFILE.md", "EDUCATION.md", "SENSITIVE_IDS.md", "photo.JPG", "transcript.pdf", "cv.pdf"):
        (about / name).write_text("x")
    (about / "documents" / "passport.pdf").write_text("x")
    (about / "documents" / "notes.md").write_text("x")
    for name in ("README.md", "INTERNSHIPS.md", "seen_companies.txt", "APPLY_BRIEF.md"):
        (intern / name).write_text("x")
    (intern / "sneaky.md").symlink_to(about / "SENSITIVE_IDS.md")
    (intern / "docs_link").symlink_to(about / "documents")
    outside = tmp_path / "outside.txt"
    outside.write_text("x")
    return about, intern


def _policy(tree, mode):
    about, intern = tree
    return make_policy(about, intern, about / "cv.pdf", mode)


def scout(tree):
    return _policy(tree, "scout")


def apply_(tree):
    return _policy(tree, "apply")


def ok(policy, tool, inp):
    allowed, reason = policy.decide(tool, inp)
    assert isinstance(reason, str) and reason
    return allowed


# --- Read ---

@pytest.mark.parametrize("path", [
    "{about}/PROFILE.md", "{about}/EDUCATION.md", "{intern}/README.md", "README.md", "../PROFILE.md",
    "seen_companies.txt", "{about}/cv.pdf", "{intern}/raw/../INTERNSHIPS.md", "{about}/new_file.md",
])
@pytest.mark.parametrize("mode", ["scout", "apply"])
def test_read_allowed(tree, mode, path):
    about, intern = tree
    assert ok(_policy(tree, mode), "Read", {"file_path": path.format(about=about, intern=intern)})


@pytest.mark.parametrize("path", [
    "{about}/SENSITIVE_IDS.md",
    "{about}/sensitive_ids.md",
    "{about}/Sensitive_Ids.MD",
    "../SENSITIVE_IDS.md",
    "{intern}/../SENSITIVE_IDS.md",
    "internships/../SENSITIVE_IDS.md",
    "{intern}/raw/../../SENSITIVE_IDS.md",
    "sneaky.md",  # symlink into SENSITIVE_IDS.md
    "{intern}/sneaky.md",
    "{about}/documents/notes.md",
    "{about}/DOCUMENTS/notes.md",
    "{intern}/docs_link/notes.md",  # symlink into documents/
    "../documents/passport.pdf",
    "{about}/photo.JPG",
    "{about}/transcript.pdf",
    "{about}/../outside.txt",
    "/etc/passwd",
    "~/.ssh/id_rsa",
    "",
])
@pytest.mark.parametrize("mode", ["scout", "apply"])
def test_read_denied(tree, mode, path):
    about, intern = tree
    assert not ok(_policy(tree, mode), "Read", {"file_path": path.format(about=about, intern=intern)})


def test_read_missing_path_denied(tree):
    assert not ok(scout(tree), "Read", {})
    assert not ok(scout(tree), "Read", {"file_path": 3})


# --- Write / Edit ---

@pytest.mark.parametrize("tool", ["Write", "Edit", "MultiEdit"])
@pytest.mark.parametrize("path", [
    "INTERNSHIPS.md", "seen_companies.txt", "raw/2026-10-14_argus.md", "{intern}/applications/acme.md",
    "{intern}/new_dir/x.md",
])
def test_scout_write_allowed(tree, tool, path):
    about, intern = tree
    assert ok(scout(tree), tool, {"file_path": path.format(about=about, intern=intern)})


@pytest.mark.parametrize("tool", ["Write", "Edit", "MultiEdit"])
@pytest.mark.parametrize("path", [
    "../PROFILE.md", "{about}/new.md", "{about}/SENSITIVE_IDS.md", "sneaky.md", "../SENSITIVE_IDS.md",
    "{intern}/../outside.md", "{intern}/docs_link/x.md", "/tmp/x.md", "{intern}/SENSITIVE_IDS.md",
])
def test_scout_write_denied(tree, tool, path):
    about, intern = tree
    assert not ok(scout(tree), tool, {"file_path": path.format(about=about, intern=intern)})


@pytest.mark.parametrize("path,allowed", [
    ("applications/acme.md", True),
    ("{intern}/applications/sub/acme.md", True),
    ("INTERNSHIPS.md", False),
    ("applications/../INTERNSHIPS.md", False),
    ("{intern}/applications", False),
    ("../PROFILE.md", False),
])
def test_apply_write_only_applications(tree, path, allowed):
    about, intern = tree
    assert ok(apply_(tree), "Write", {"file_path": path.format(about=about, intern=intern)}) is allowed
    assert ok(apply_(tree), "Edit", {"file_path": path.format(about=about, intern=intern)}) is allowed


def test_notebook_edit_denied(tree):
    assert not ok(scout(tree), "NotebookEdit", {"notebook_path": "INTERNSHIPS.md"})


# --- Web / search tools ---

@pytest.mark.parametrize("tool", ["WebSearch", "WebFetch"])
def test_web_scout_only(tree, tool):
    assert ok(scout(tree), tool, {"query": "x", "url": "https://example.com"})
    assert not ok(apply_(tree), tool, {"query": "x", "url": "https://example.com"})


@pytest.mark.parametrize("mode", ["scout", "apply"])
def test_toolsearch_always(tree, mode):
    assert ok(_policy(tree, mode), "ToolSearch", {"query": "select:x"})


@pytest.mark.parametrize("inp,allowed", [
    ({"pattern": "*.md"}, True),
    ({"pattern": "**/*.md", "path": "{about}"}, True),
    ({"pattern": "*", "path": ".."}, True),
    ({"pattern": "*", "path": "/etc"}, False),
    ({"pattern": "*", "path": "{about}/.."}, False),
    ({"pattern": "../../*"}, False),
    ({"pattern": "/etc/*"}, False),
    ({"pattern": "*", "path": "{about}/documents"}, False),
])
def test_glob_scout(tree, inp, allowed):
    about, intern = tree
    inp = {k: v.format(about=about) for k, v in inp.items()}
    assert ok(scout(tree), "Glob", inp) is allowed
    assert not ok(apply_(tree), "Glob", inp)


@pytest.mark.parametrize("inp,allowed", [
    ({"pattern": "Acme"}, False),  # cwd = internships contains a symlink to SENSITIVE_IDS.md
    ({"pattern": "Acme", "path": "raw"}, True),
    ({"pattern": "Acme", "path": "{about}/PROFILE.md"}, True),
    ({"pattern": "[0-9]+", "path": "{about}"}, False),  # would scan SENSITIVE_IDS.md
    ({"pattern": "x", "path": ".."}, False),
    ({"pattern": "x", "path": "{about}/SENSITIVE_IDS.md"}, False),
    ({"pattern": "x", "path": "sneaky.md"}, False),
    ({"pattern": "x", "path": "/etc"}, False),
])
def test_grep_scout(tree, inp, allowed):
    about, intern = tree
    inp = {k: v.format(about=about) for k, v in inp.items()}
    assert ok(scout(tree), "Grep", inp) is allowed
    assert not ok(apply_(tree), "Grep", inp)


@pytest.mark.parametrize("tool", ["Bash", "Task", "Agent", "TodoWrite", "mcp__other__thing", "Skill", "KillShell"])
@pytest.mark.parametrize("mode", ["scout", "apply"])
def test_everything_else_denied(tree, mode, tool):
    assert not ok(_policy(tree, mode), tool, {"command": "ls"})


# --- Chrome (apply mode) ---

@pytest.mark.parametrize("tool", [
    "mcp__claude-in-chrome__tabs_context_mcp", "mcp__claude-in-chrome__navigate",
    "mcp__claude-in-chrome__computer", "mcp__claude-in-chrome__form_input",
    "mcp__claude-in-chrome__read_page", "mcp__claude-in-chrome__tabs_create_mcp",
])
def test_chrome_apply_only(tree, tool):
    assert ok(apply_(tree), tool, {})
    assert not ok(scout(tree), tool, {})


@pytest.mark.parametrize("tool", [
    "mcp__claude-in-chrome__select_browser", "mcp__claude-in-chrome__switch_browser",
    "mcp__claude-in-chrome__upload_image",
])
def test_chrome_forbidden_tools(tree, tool):
    assert not ok(apply_(tree), tool, {"imageId": "x", "tabId": 1})


@pytest.mark.parametrize("paths,allowed", [
    (["{about}/cv.pdf"], True),
    (["{about}/transcript.pdf"], True),
    (["{about}/cv.pdf", "{about}/transcript.pdf"], True),
    (["{about}/SENSITIVE_IDS.md"], False),
    (["{about}/PROFILE.md"], False),
    (["{about}/documents/passport.pdf"], False),
    (["{about}/DOCUMENTS/passport.pdf"], False),
    (["{about}/cv.pdf", "{about}/SENSITIVE_IDS.md"], False),
    (["{about}/internships/../SENSITIVE_IDS.md"], False),
    (["{about}/photo.JPG"], False),
    (["/etc/passwd"], False),
    ([], False),
])
def test_file_upload(tree, paths, allowed):
    about, intern = tree
    inp = {"paths": [p.format(about=about) for p in paths], "ref": "ref_1", "tabId": 1}
    assert ok(apply_(tree), "mcp__claude-in-chrome__file_upload", inp) is allowed
    assert not ok(scout(tree), "mcp__claude-in-chrome__file_upload", inp)


def test_file_upload_symlink_to_sensitive_pdf(tree):
    about, intern = tree
    (about / "fake.pdf").symlink_to(about / "SENSITIVE_IDS.md")
    inp = {"paths": [str(about / "fake.pdf")], "ref": "r", "tabId": 1}
    assert not ok(apply_(tree), "mcp__claude-in-chrome__file_upload", inp)


def test_file_upload_bad_input(tree):
    assert not ok(apply_(tree), "mcp__claude-in-chrome__file_upload", {"paths": "cv.pdf"})


# --- async SDK callback ---

async def test_callback_returns_sdk_types(tree):
    about, intern = tree
    policy = scout(tree)
    ctx = ToolPermissionContext()
    allow = await policy("Read", {"file_path": str(about / "PROFILE.md")}, ctx)
    assert isinstance(allow, PermissionResultAllow)
    deny = await policy("Read", {"file_path": str(about / "SENSITIVE_IDS.md")}, ctx)
    assert isinstance(deny, PermissionResultDeny) and deny.message


async def test_pre_tool_use_hook(tree):
    about, intern = tree
    policy = scout(tree)
    out = await policy.pre_tool_use_hook(
        {"hook_event_name": "PreToolUse", "tool_name": "Bash", "tool_input": {"command": "ls"}}, "t1", {}
    )
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"
    out = await policy.pre_tool_use_hook(
        {"hook_event_name": "PreToolUse", "tool_name": "Read", "tool_input": {"file_path": "README.md"}}, "t2", {}
    )
    assert out == {}


def test_bad_mode(tree):
    about, intern = tree
    with pytest.raises(ValueError):
        make_policy(about, intern, about / "cv.pdf", "other")


def test_grep_cwd_allowed_without_escaping_symlinks(tree):
    about, intern = tree
    (intern / "sneaky.md").unlink()
    (intern / "docs_link").unlink()
    (intern / "ok_link.md").symlink_to(intern / "README.md")
    assert ok(scout(tree), "Grep", {"pattern": "Acme"})
