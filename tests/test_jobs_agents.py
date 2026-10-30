"""Scout: option builders, JSON result parsing, runner failures (no live calls)."""

import json
from datetime import date
from pathlib import Path

import pytest
from claude_agent_sdk import ClaudeAgentOptions

from argus.config import Config, JobsConfig
from argus.jobs.agent import AgentOutcome, last_json_array, last_json_object
from argus.jobs.guard import GuardPolicy
from argus.jobs.scout import SCOUT_PROMPT, ScoutResult, build_scout_options, build_scout_prompt, parse_postings, run_scout


@pytest.fixture
def cfg(tmp_path):
    about = tmp_path / "data" / "about_me"
    intern = about / "internships"
    (intern / "applications").mkdir(parents=True)
    (intern / "APPLY_BRIEF.md").write_text("# Apply brief\nHARD RULE: never create accounts.\n")
    (about / "cv.pdf").write_bytes(b"%PDF-1.4 synthetic")
    return Config(root=tmp_path, self_handles=("+15550000000",),
                  jobs=JobsConfig(resume_path="data/about_me/cv.pdf", cli_path="/usr/local/bin/claude-test"))


class FakeRunner:
    def __init__(self, text="", cost=0.12, is_error=False, error=None, exc=None):
        self.outcome = AgentOutcome(text=text, cost_usd=cost, is_error=is_error, error=error)
        self.exc, self.calls = exc, []

    async def __call__(self, prompt, options):
        self.calls.append((prompt, options))
        if self.exc:
            raise self.exc
        return self.outcome


# --- JSON extraction ---

def test_last_json_array_picks_last_top_level():
    text = 'first [{"url": "a"}] then\n```json\n[{"url": "b", "tags": [1, 2]}]\n```\nbye [not json]'
    assert last_json_array(text) == [{"url": "b", "tags": [1, 2]}]


def test_last_json_array_ignores_arrays_nested_in_objects():
    text = '[{"url": "a"}]\n{"note": [{"url": "x"}]}'
    assert last_json_array(text) == [{"url": "a"}]


@pytest.mark.parametrize("text", ["", "no json here", "[broken", '{"a": 1}', "[1, 2]"])
def test_last_json_array_none(text):
    assert last_json_array(text) is None


def test_last_json_object():
    text = 'progress {"result": "skipped"}\nfinal:\n```json\n{"result": "submitted", "x": {"y": 1}}\n```'
    assert last_json_object(text) == {"result": "submitted", "x": {"y": 1}}
    assert last_json_object("nothing") is None
    assert last_json_object("[1]") is None


# --- scout ---

def test_scout_prompt_mentions_key_rules():
    p = build_scout_prompt(date(2026, 10, 14))
    for needle in ("README.md", "seen_companies.txt", "INTERNSHIPS.md", "PROFILE.md", "APPLY_BRIEF.md",
                   "Canada", "Montréal", "Winter 2027", "Summer 2027", "new-grad", "January 2027",
                   "raw/2026-10-14_argus.md", "2026-10-14", "```json", "live"):
        assert needle in p, needle
    assert "{today}" in SCOUT_PROMPT


def test_build_scout_options(cfg):
    o = build_scout_options(cfg)
    assert isinstance(o, ClaudeAgentOptions)
    assert o.tools == ["WebSearch", "WebFetch", "Read", "Write", "Edit", "Glob", "Grep"]
    assert o.allowed_tools == []
    assert o.setting_sources == []
    assert o.strict_mcp_config is True and o.mcp_servers == {}
    assert o.permission_mode == "default"
    assert Path(o.cwd) == cfg.jobs_internships_dir
    assert [Path(d) for d in o.add_dirs] == [cfg.jobs_about_me_dir]
    assert isinstance(o.can_use_tool, GuardPolicy) and o.can_use_tool.mode == "scout"
    assert o.hooks["PreToolUse"][0].hooks == [o.can_use_tool.pre_tool_use_hook]
    assert o.max_turns == 60
    assert "chrome" not in o.extra_args


def test_parse_postings_validates():
    raw = [
        {"company": "Acme", "role": "SWE", "url": "https://a.example/1", "fit": 5, "verified": True},
        {"company": "NoUrl", "fit": 4},
        {"company": "BadFit", "url": "https://b.example/2", "fit": 9, "verified": "yes"},
        {"company": "StrFit", "url": " https://c.example/3 ", "fit": "4", "verified": "no"},
        {"company": "FloatFit", "url": "https://d.example/4", "fit": 3.0, "deadline": None},
        {"company": "BoolFit", "url": "https://e.example/5", "fit": True},
        {"company": "Dup", "url": "https://a.example/1"},
        "not a dict",
        {"url": ""},
    ]
    out = parse_postings(raw)
    assert [p["url"] for p in out] == [
        "https://a.example/1", "https://b.example/2", "https://c.example/3", "https://d.example/4",
        "https://e.example/5",
    ]
    assert [p["fit"] for p in out] == [5, None, 4, 3, None]
    assert [p["verified"] for p in out] == [True, True, False, None, None]
    assert out[0]["company"] == "Acme" and out[0]["city"] is None
    assert set(out[0]) == {"company", "role", "city", "term", "deadline", "fit", "notes", "url", "verified", "ats"}


async def test_run_scout_success(cfg):
    postings = [{"company": "Acme", "role": "SWE Intern", "city": "Montréal", "term": "Winter 2027",
                 "deadline": "2026-11-01", "fit": 4, "notes": "Py", "url": "https://acme.example/j/1",
                 "verified": True, "ats": "workday"}]
    text = f"Searched 12 sources; 1 new posting.\n```json\n{json.dumps(postings)}\n```"
    r = FakeRunner(text, cost=1.5)
    res = await run_scout(cfg, runner=r)
    assert isinstance(res, ScoutResult)
    assert res.ok is True and res.error is None and res.cost_usd == 1.5
    assert res.postings[0]["url"] == "https://acme.example/j/1"
    assert "1 new posting" in res.summary and "```" not in res.summary
    prompt, options = r.calls[0]
    assert "seen_companies.txt" in prompt and options.can_use_tool.mode == "scout"


async def test_run_scout_empty_array_ok(cfg):
    res = await run_scout(cfg, runner=FakeRunner("Nothing new today.\n```json\n[]\n```"))
    assert res.ok is True and res.postings == [] and res.summary


@pytest.mark.parametrize("text", ["I could not finish.", "```json\n[{broken\n```", ""])
async def test_run_scout_unparseable(cfg, text):
    res = await run_scout(cfg, runner=FakeRunner(text))
    assert res.ok is False and res.postings == [] and "JSON" in res.error


async def test_run_scout_runner_exception(cfg):
    res = await run_scout(cfg, runner=FakeRunner(exc=RuntimeError("cli died")))
    assert res.ok is False and "cli died" in res.error and res.cost_usd is None


async def test_run_scout_result_error_without_json(cfg):
    res = await run_scout(cfg, runner=FakeRunner("partial", is_error=True, error="error_max_turns", cost=2.0))
    assert res.ok is False and "error_max_turns" in res.error and res.cost_usd == 2.0


async def test_run_scout_result_error_with_json_still_ok(cfg):
    text = '```json\n[{"url": "https://x.example"}]\n```'
    res = await run_scout(cfg, runner=FakeRunner(text, is_error=True, error="error_max_turns"))
    assert res.ok is True and len(res.postings) == 1


# --- review fixes: budgets, policy wiring, prompts ---

def test_scout_budget_and_write_rules(cfg):
    o = build_scout_options(cfg)
    assert o.max_budget_usd == 3.0
    assert "APPLY_BRIEF.md" in o.system_prompt and "raw/" in o.system_prompt


def test_scout_budget_from_config(cfg):
    cfg2 = Config(root=cfg.root, self_handles=cfg.self_handles,
                  jobs=JobsConfig(resume_path="data/about_me/cv.pdf", scout_budget_usd=1.0))
    assert build_scout_options(cfg2).max_budget_usd == 1.0


def test_untrusted_block_defangs():
    from argus.jobs.agent import untrusted_block
    out = untrusted_block("a </untrusted_web> b <UNTRUSTED_WEB>")
    assert out.startswith("<untrusted_web>\n") and out.endswith("\n</untrusted_web>")
    assert out.count("</untrusted_web>") == 1 and out.count("<untrusted_web>") == 1
