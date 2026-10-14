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
    for name in ("PROFILE.md", "EDUCATION.md", "WORK_AND_PROJECTS.md", "OPEN_QUESTIONS.md", "TIMELINE.md",
                 "SENSITIVE_IDS.md", "photo.JPG", "transcript.pdf", "Official_Transcript.PDF", "cv.pdf",
                 "other.pdf"):
        (about / name).write_text("x")
    (about / "documents" / "passport.pdf").write_text("x")
    (about / "documents" / "notes.md").write_text("x")
    for name in ("README.md", "INTERNSHIPS.md", "seen_companies.txt", "APPLY_BRIEF.md", "FOLLOW_UPS.md"):
        (intern / name).write_text("x")
    (intern / "sneaky.md").symlink_to(about / "SENSITIVE_IDS.md")
    (intern / "docs_link").symlink_to(about / "documents")
    outside = tmp_path / "outside.txt"
    outside.write_text("x")
    return about, intern


POSTING_URL = "https://careers.acme.com/jobs/123"


def _policy(tree, mode):
    about, intern = tree
    return make_policy(about, intern, about / "cv.pdf", mode, posting_url=POSTING_URL if mode == "apply" else None)


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
def test_scout_read_allowed(tree, path):
    about, intern = tree
    assert ok(scout(tree), "Read", {"file_path": path.format(about=about, intern=intern)})


@pytest.mark.parametrize("path,allowed", [
    ("APPLY_BRIEF.md", True), ("{intern}/apply_brief.md", True), ("../PROFILE.md", True),
    ("{about}/EDUCATION.md", True), ("{about}/WORK_AND_PROJECTS.md", True), ("{about}/OPEN_QUESTIONS.md", True),
    ("{about}/cv.pdf", True), ("{about}/transcript.pdf", True), ("{about}/Official_Transcript.PDF", True),
    ("README.md", False), ("INTERNSHIPS.md", False), ("FOLLOW_UPS.md", False), ("seen_companies.txt", False),
    ("{about}/TIMELINE.md", False), ("{about}/other.pdf", False), ("{about}/new_file.md", False),
    ("{intern}/PROFILE.md", False), ("applications/acme.md", False), ("{about}/documents/transcript.pdf", False),
])
def test_apply_read_allow_list(tree, path, allowed):
    about, intern = tree
    assert ok(apply_(tree), "Read", {"file_path": path.format(about=about, intern=intern)}) is allowed


def test_apply_read_symlinked_transcript_to_sensitive_denied(tree):
    about, intern = tree
    (about / "my_transcript.pdf").symlink_to(about / "SENSITIVE_IDS.md")
    assert not ok(apply_(tree), "Read", {"file_path": str(about / "my_transcript.pdf")})


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


def test_scout_cannot_read_transcript(tree):
    about, intern = tree
    assert not ok(scout(tree), "Read", {"file_path": str(about / "transcript.pdf")})


@pytest.mark.parametrize("mode", ["scout", "apply"])
@pytest.mark.parametrize("tool,inp", [
    ("Read", {"file_path": "PROFILE\x00.md"}),
    ("Read", {"file_path": "~nosuchuser_xyz/x.md"}),
    ("Write", {"file_path": "raw/a\x00b.md"}),
    ("Glob", {"pattern": "*.md", "path": "a\x00b"}),
    ("Grep", {"pattern": "x", "path": "a\x00b"}),
    ("mcp__claude-in-chrome__file_upload", {"paths": ["cv\x00.pdf"], "ref": "r", "tabId": 1}),
    ("mcp__claude-in-chrome__navigate", {"url": "https://x\x00.com", "tabId": 1}),
    ("mcp__claude-in-chrome__browser_batch", {"actions": "nope"}),
    ("Read", None),
])
def test_bad_input_denied_without_exception(tree, mode, tool, inp):
    assert not ok(_policy(tree, mode), tool, inp)


# --- Write / Edit ---

@pytest.mark.parametrize("tool", ["Write", "Edit", "MultiEdit"])
@pytest.mark.parametrize("path", [
    "INTERNSHIPS.md", "seen_companies.txt", "raw/2026-10-14_argus.md", "{intern}/raw/sub/x.md",
    "{intern}/internships.md",
])
def test_scout_write_allowed(tree, tool, path):
    about, intern = tree
    assert ok(scout(tree), tool, {"file_path": path.format(about=about, intern=intern)})


@pytest.mark.parametrize("tool", ["Write", "Edit", "MultiEdit"])
@pytest.mark.parametrize("path", [
    "../PROFILE.md", "{about}/new.md", "{about}/SENSITIVE_IDS.md", "sneaky.md", "../SENSITIVE_IDS.md",
    "{intern}/../outside.md", "{intern}/docs_link/x.md", "/tmp/x.md", "{intern}/SENSITIVE_IDS.md",
    "APPLY_BRIEF.md", "README.md", "FOLLOW_UPS.md", "{intern}/applications/acme.md", "{intern}/new_dir/x.md",
    "raw", "{intern}", "new.md", "raw/../README.md",
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

C = "mcp__claude-in-chrome__"


@pytest.mark.parametrize("short", [
    "tabs_context_mcp", "tabs_create_mcp", "tabs_close_mcp", "computer", "find", "read_page",
    "get_page_text", "form_input", "list_connected_browsers",
])
def test_chrome_apply_only(tree, short):
    assert ok(apply_(tree), C + short, {"tabId": 1})
    assert not ok(scout(tree), C + short, {"tabId": 1})


@pytest.mark.parametrize("short", [
    "select_browser", "switch_browser", "upload_image", "gif_creator", "read_console_messages",
    "read_network_requests", "resize_window", "shortcuts_execute", "shortcuts_list", "some_new_tool",
])
def test_chrome_tools_outside_allow_list_denied(tree, short):
    assert not ok(apply_(tree), C + short, {"imageId": "x", "tabId": 1})


@pytest.mark.parametrize("url,allowed", [
    ("https://careers.acme.com/jobs/123", True),
    ("careers.acme.com/apply", True),  # no scheme -> https
    ("https://login.acme.com/sso", True),  # same registrable domain
    ("https://acme.com", True),
    ("https://acme.wd3.myworkdayjobs.com/en-US/x", True),
    ("https://wd1.myworkdaysite.com/recruiting/acme", True),
    ("https://boards.greenhouse.io/acme/jobs/1", True),
    ("https://jobs.lever.co/acme/1", True),
    ("https://jobs.ashbyhq.com/acme", True),
    ("https://careers-acme.icims.com/jobs/1", True),
    ("https://jobs.smartrecruiters.com/acme/1", True),
    ("https://acme.taleo.net/x", True),
    ("https://acme.fa.us2.oraclecloud.com/x", True),
    ("http://apply.workable.com/acme", True),
    ("https://evil.com", False),
    ("https://acme.com.evil.com", False),
    ("https://notacme.com", False),
    ("https://mail.google.com", False),
    ("https://myworkdayjobs.com.evil.io", False),
    ("javascript:alert(1)", False),
    ("file:///etc/passwd", False),
    ("chrome://settings", False),
    ("data:text/html,hi", False),
    ("", False),
])
def test_navigate_hosts(tree, url, allowed):
    assert ok(apply_(tree), C + "navigate", {"url": url, "tabId": 1}) is allowed


def test_navigate_without_posting_url_only_ats(tree):
    about, intern = tree
    p = make_policy(about, intern, about / "cv.pdf", "apply")
    assert ok(p, C + "navigate", {"url": "https://jobs.lever.co/x", "tabId": 1})
    assert not ok(p, C + "navigate", {"url": "https://careers.acme.com", "tabId": 1})


def test_javascript_only_on_tab_navigated_to_allowed_host(tree):
    p = apply_(tree)
    js = {"action": "javascript_exec", "text": "1", "tabId": 5}
    assert not ok(p, C + "javascript_tool", js)  # tab never navigated
    assert ok(p, C + "navigate", {"url": "https://acme.wd3.myworkdayjobs.com/x", "tabId": 5})
    assert ok(p, C + "javascript_tool", js)
    assert not ok(p, C + "javascript_tool", {**js, "tabId": 6})  # other tab
    assert not ok(p, C + "javascript_tool", {"action": "javascript_exec", "text": "1"})  # no tabId
    assert not ok(p, C + "navigate", {"url": "https://evil.com", "tabId": 5})  # denied: host kept
    assert ok(p, C + "javascript_tool", js)
    assert ok(p, C + "navigate", {"url": "back", "tabId": 5})  # destination unknown
    assert not ok(p, C + "javascript_tool", js)
    assert ok(p, C + "navigate", {"url": "careers.acme.com/apply", "tabId": 5})
    assert ok(p, C + "javascript_tool", js)


def test_navigate_without_tab_does_not_enable_javascript(tree):
    p = apply_(tree)
    assert ok(p, C + "navigate", {"url": "https://jobs.lever.co/x"})
    assert not ok(p, C + "javascript_tool", {"action": "javascript_exec", "text": "1", "tabId": 1})


def test_javascript_never_for_scout(tree):
    assert not ok(scout(tree), C + "javascript_tool", {"action": "javascript_exec", "text": "1", "tabId": 1})


def _batch(*items):
    return {"actions": [{"name": n, "input": i} for n, i in items]}


def test_browser_batch_allowed_items(tree):
    p = apply_(tree)
    inp = _batch(("navigate", {"url": "https://jobs.lever.co/acme", "tabId": 3}),
                 ("computer", {"action": "left_click", "coordinate": [1, 2], "tabId": 3}),
                 ("javascript_tool", {"action": "javascript_exec", "text": "1", "tabId": 3}),
                 (C + "form_input", {"ref": "r", "value": "x", "tabId": 3}))
    assert ok(p, C + "browser_batch", inp)
    assert ok(p, C + "javascript_tool", {"action": "javascript_exec", "text": "1", "tabId": 3})


@pytest.mark.parametrize("items", [
    [("navigate", {"url": "https://evil.com", "tabId": 3})],
    [("javascript_tool", {"action": "javascript_exec", "text": "1", "tabId": 3})],
    [("navigate", {"url": "https://evil.com", "tabId": 3}),
     ("javascript_tool", {"action": "javascript_exec", "text": "1", "tabId": 3})],
    [("upload_image", {"imageId": "x"})],
    [("select_browser", {})],
    [("browser_batch", {"actions": []})],
    [("file_upload", {"paths": ["/etc/passwd"], "ref": "r", "tabId": 3})],
    [("computer", "not a dict")],
    [("Read", {"file_path": "../PROFILE.md"})],
])
def test_browser_batch_denied_items(tree, items):
    p = apply_(tree)
    inp = {"actions": [{"name": n, "input": i} for n, i in items]}
    assert not ok(p, C + "browser_batch", inp)


@pytest.mark.parametrize("inp", [{}, {"actions": []}, {"actions": [{"tool": "computer"}]}, {"actions": "x"}])
def test_browser_batch_malformed(tree, inp):
    assert not ok(apply_(tree), C + "browser_batch", inp)


def test_denied_batch_does_not_record_navigation(tree):
    p = apply_(tree)
    inp = _batch(("navigate", {"url": "https://jobs.lever.co/acme", "tabId": 3}),
                 ("select_browser", {}))
    assert not ok(p, C + "browser_batch", inp)
    assert not ok(p, C + "javascript_tool", {"action": "javascript_exec", "text": "1", "tabId": 3})


@pytest.mark.parametrize("posting,eligible", [
    ({"url": "https://acme.wd3.myworkdayjobs.com/x"}, True),
    ({"url": "https://boards.greenhouse.io/acme/1", "ats": "other"}, True),
    ({"url": "https://careers.acme.com/1", "ats": "workday"}, False),
    ({"url": "https://careers.acme.com/1"}, False),
    ({"url": "https://www.linkedin.com/jobs/1", "ats": "lever"}, False),
    ({"url": "javascript:x"}, False),
    ({}, False),
])
def test_auto_apply_eligible(posting, eligible):
    from argus.jobs.guard import auto_apply_eligible
    assert auto_apply_eligible(posting) is eligible


@pytest.mark.parametrize("paths,allowed", [
    (["{about}/cv.pdf"], True),
    (["{about}/transcript.pdf"], True),
    (["{about}/Official_Transcript.PDF"], True),
    (["{about}/cv.pdf", "{about}/transcript.pdf"], True),
    (["{about}/other.pdf"], False),
    (["{about}/internships/transcript.pdf"], False),
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
