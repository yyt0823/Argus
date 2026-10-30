from pathlib import Path
import pytest
from argus.config import load_config

def test_load_config_reads_handles_and_defaults(tmp_path: Path):
    (tmp_path / "argus.toml").write_text('self_handles = ["+15550000000", "me@example.com"]\npoll_interval = 2.5\n')
    cfg = load_config(tmp_path)
    assert cfg.self_handles == ("+15550000000", "me@example.com")
    assert cfg.poll_interval == 2.5
    assert cfg.rollover_hour == 4
    assert cfg.db_path == tmp_path / "data" / "argus.db"
    assert cfg.memory_dir == tmp_path / "memory"

def test_load_config_missing_file_raises(tmp_path: Path):
    with pytest.raises(ValueError, match="argus.toml"):
        load_config(tmp_path)

def test_load_config_requires_handles(tmp_path: Path):
    (tmp_path / "argus.toml").write_text("self_handles = []\n")
    with pytest.raises(ValueError, match="self_handles"):
        load_config(tmp_path)

def test_load_config_mode_defaults_to_self(tmp_path: Path):
    (tmp_path / "argus.toml").write_text('self_handles = ["+15550000000"]\n')
    assert load_config(tmp_path).mode == "self"

def test_load_config_reads_contact_mode(tmp_path: Path):
    (tmp_path / "argus.toml").write_text('mode = "contact"\nself_handles = ["+15550000000"]\n')
    assert load_config(tmp_path).mode == "contact"

@pytest.mark.parametrize("bad", ['"bot"', '""', "1"])
def test_load_config_rejects_bad_mode(tmp_path: Path, bad):
    (tmp_path / "argus.toml").write_text(f'mode = {bad}\nself_handles = ["+15550000000"]\n')
    with pytest.raises(ValueError, match="mode"):
        load_config(tmp_path)

# --- [mail] table ---

from datetime import time
from argus.config import MailConfig

def _write(tmp_path, extra=""):
    (tmp_path / "argus.toml").write_text('self_handles = ["+15550000000"]\n' + extra)

def test_mail_defaults(tmp_path: Path):
    _write(tmp_path)
    m = load_config(tmp_path).mail
    assert m == MailConfig()
    assert m.enabled is True and m.briefing_time == time(8, 0)
    assert m.quiet_start == time(23, 0) and m.quiet_end == time(8, 0)
    assert m.check_interval_minutes == 5.0 and m.triage_model == "haiku"

def test_mail_table_parsed(tmp_path: Path):
    _write(tmp_path, '[mail]\nenabled = false\nbriefing_time = "07:30"\nquiet_start = "22:00"\n'
                     'quiet_end = "06:45"\ncheck_interval_minutes = 2\ntriage_model = "sonnet"\n')
    m = load_config(tmp_path).mail
    assert m == MailConfig(enabled=False, briefing_time=time(7, 30), quiet_start=time(22, 0),
                           quiet_end=time(6, 45), check_interval_minutes=2.0, triage_model="sonnet")
    assert isinstance(m.check_interval_minutes, float)

@pytest.mark.parametrize("line", [
    'briefing_time = "8am"', 'briefing_time = "25:00"', 'quiet_start = "23:60"', 'quiet_end = 8',
    'quiet_end = "8:00:00"', 'enabled = "yes"', 'check_interval_minutes = 0',
    'check_interval_minutes = "5"', 'check_interval_minutes = true', 'triage_model = ""', 'triage_model = 3',
    'unknown_key = 1',
])
def test_mail_bad_values_rejected(tmp_path: Path, line):
    _write(tmp_path, f"[mail]\n{line}\n")
    with pytest.raises(ValueError, match="mail"):
        load_config(tmp_path)

def test_mail_must_be_table(tmp_path: Path):
    _write(tmp_path, 'mail = "on"\n')
    with pytest.raises(ValueError, match="mail"):
        load_config(tmp_path)

@pytest.mark.parametrize("t,quiet", [
    (time(23, 0), True), (time(23, 30), True), (time(0, 0), True), (time(7, 59), True),
    (time(8, 0), False), (time(12, 0), False), (time(22, 59), False),
])
def test_in_quiet_hours_across_midnight(t, quiet):
    assert MailConfig().in_quiet_hours(t) is quiet

@pytest.mark.parametrize("t,quiet", [
    (time(12, 59), False), (time(13, 0), True), (time(13, 59), True), (time(14, 0), False),
])
def test_in_quiet_hours_same_day_range(t, quiet):
    assert MailConfig(quiet_start=time(13, 0), quiet_end=time(14, 0)).in_quiet_hours(t) is quiet

def test_in_quiet_hours_empty_range():
    assert MailConfig(quiet_start=time(8, 0), quiet_end=time(8, 0)).in_quiet_hours(time(8, 0)) is False

# --- [fitness] table ---

from argus.config import FitnessConfig

def test_fitness_defaults(tmp_path: Path):
    _write(tmp_path)
    cfg = load_config(tmp_path)
    f = cfg.fitness
    assert f == FitnessConfig()
    assert f.enabled is True and f.plan_path == "data/fitness/CLAUDE.md"
    assert f.training_days == ("mon", "wed", "fri")
    assert (f.training_kcal, f.rest_kcal, f.min_kcal) == (2000, 1800, 1200)
    assert f.protein_g == (100, 150) and f.carbs_g == (150, 250) and f.fat_g == (50, 70)
    assert f.added_sugar_g_max == 25 and f.sodium_mg_max == 2300
    assert cfg.fitness_plan_path == tmp_path / "data" / "fitness" / "CLAUDE.md"

def test_fitness_table_parsed(tmp_path: Path):
    _write(tmp_path, '[fitness]\nenabled = false\nplan_path = "plan.md"\ntraining_days = ["Tue", "sat"]\n'
                     'training_kcal = 1800\nrest_kcal = 1600\nmin_kcal = 1500\nprotein_g = [160, 170]\n'
                     'carbs_g = [100, 150.5]\nfat_g = [40, 50]\nadded_sugar_g_max = 20\nsodium_mg_max = 2000\n')
    cfg = load_config(tmp_path)
    assert cfg.fitness == FitnessConfig(
        enabled=False, plan_path="plan.md", training_days=("tue", "sat"), training_kcal=1800,
        rest_kcal=1600, min_kcal=1500, protein_g=(160, 170), carbs_g=(100, 150.5), fat_g=(40, 50),
        added_sugar_g_max=20, sodium_mg_max=2000)
    assert isinstance(cfg.fitness.protein_g, tuple)
    assert cfg.fitness_plan_path == tmp_path / "plan.md"

def test_fitness_absolute_plan_path(tmp_path: Path):
    _write(tmp_path, f'[fitness]\nplan_path = "{tmp_path / "elsewhere.md"}"\n')
    assert load_config(tmp_path).fitness_plan_path == tmp_path / "elsewhere.md"

@pytest.mark.parametrize("line,key", [
    ('enabled = "yes"', "enabled"), ('plan_path = ""', "plan_path"), ("plan_path = 3", "plan_path"),
    ('training_days = "mon"', "training_days"), ('training_days = ["monday"]', "training_days"),
    ("training_days = [1]", "training_days"),
    ("training_kcal = 0", "training_kcal"), ('rest_kcal = "1600"', "rest_kcal"), ("min_kcal = -1", "min_kcal"),
    ("training_kcal = true", "training_kcal"),
    ("protein_g = 160", "protein_g"), ("protein_g = [170, 160]", "protein_g"), ("carbs_g = [1, 2, 3]", "carbs_g"),
    ('fat_g = ["a", 2]', "fat_g"), ("fat_g = [-1, 2]", "fat_g"),
    ("added_sugar_g_max = -5", "added_sugar_g_max"), ("sodium_mg_max = false", "sodium_mg_max"),
    ("bogus = 1", "bogus"),
])
def test_fitness_bad_values_rejected(tmp_path: Path, line, key):
    _write(tmp_path, f"[fitness]\n{line}\n")
    with pytest.raises(ValueError, match=f"fitness.{key}"):
        load_config(tmp_path)

def test_fitness_must_be_table(tmp_path: Path):
    _write(tmp_path, 'fitness = 1\n')
    with pytest.raises(ValueError, match="fitness"):
        load_config(tmp_path)

# --- [jobs] table ---

from argus.config import JobsConfig

def test_jobs_defaults(tmp_path: Path):
    _write(tmp_path)
    cfg = load_config(tmp_path)
    j = cfg.jobs
    assert j == JobsConfig()
    assert j.enabled is True and j.auto_apply is False  # opt-in
    assert j.scout_budget_usd == 3.0 and j.apply_budget_usd == 2.0
    assert j.scout_time == time(7, 30)
    assert j.min_fit_apply == 4 and j.daily_apply_limit == 8
    assert j.apply_window == (time(8, 30), time(22, 0))
    assert j.cli_path == "/opt/homebrew/bin/claude"
    assert cfg.jobs_internships_dir == tmp_path / "data" / "about_me" / "internships"
    assert cfg.jobs_resume_path == tmp_path / "data" / "about_me" / "yantian_yin_cv.pdf"
    assert cfg.jobs_about_me_dir == tmp_path / "data" / "about_me"

def test_jobs_table_parsed(tmp_path: Path):
    _write(tmp_path, '[jobs]\nenabled = false\nscout_time = "06:05"\nauto_apply = false\nmin_fit_apply = 5\n'
                     'daily_apply_limit = 0\napply_window = ["09:00", "21:30"]\n'
                     'internships_dir = "x/intern"\nresume_path = "/abs/cv.pdf"\ncli_path = "/usr/local/bin/claude"\n'
                     'scout_budget_usd = 5\napply_budget_usd = 1.5\n')
    cfg = load_config(tmp_path)
    assert cfg.jobs == JobsConfig(
        enabled=False, scout_time=time(6, 5), auto_apply=False, min_fit_apply=5, daily_apply_limit=0,
        apply_window=(time(9, 0), time(21, 30)), internships_dir="x/intern", resume_path="/abs/cv.pdf",
        cli_path="/usr/local/bin/claude", scout_budget_usd=5.0, apply_budget_usd=1.5,
    )
    assert isinstance(cfg.jobs.scout_budget_usd, float)
    assert cfg.jobs_internships_dir == tmp_path / "x" / "intern"
    assert cfg.jobs_resume_path == Path("/abs/cv.pdf")
    assert cfg.jobs_about_me_dir == tmp_path / "x"

def test_jobs_auto_apply_enabled_explicitly(tmp_path: Path):
    _write(tmp_path, '[jobs]\nauto_apply = true\n')
    assert load_config(tmp_path).jobs.auto_apply is True

def test_jobs_in_apply_window():
    j = JobsConfig()
    assert j.in_apply_window(time(8, 30)) and j.in_apply_window(time(21, 59))
    assert not j.in_apply_window(time(22, 0)) and not j.in_apply_window(time(7, 0))

@pytest.mark.parametrize("extra", [
    'jobs = 1\n',
    '[jobs]\nbogus = 1\n',
    '[jobs]\nenabled = "yes"\n',
    '[jobs]\nauto_apply = 1\n',
    '[jobs]\nscout_time = "7:30"\n',
    '[jobs]\nscout_time = "24:00"\n',
    '[jobs]\nmin_fit_apply = 0\n',
    '[jobs]\nmin_fit_apply = 6\n',
    '[jobs]\nmin_fit_apply = 4.5\n',
    '[jobs]\nmin_fit_apply = true\n',
    '[jobs]\ndaily_apply_limit = -1\n',
    '[jobs]\napply_window = ["08:30"]\n',
    '[jobs]\napply_window = ["08:30", "25:00"]\n',
    '[jobs]\napply_window = ["22:00", "08:30"]\n',
    '[jobs]\napply_window = "08:30-22:00"\n',
    '[jobs]\ninternships_dir = ""\n',
    '[jobs]\nresume_path = 3\n',
    '[jobs]\ncli_path = "  "\n',
    '[jobs]\nscout_budget_usd = 0\n',
    '[jobs]\nscout_budget_usd = -1\n',
    '[jobs]\napply_budget_usd = "2"\n',
    '[jobs]\napply_budget_usd = true\n',
    '[jobs]\napply_budget_usd = inf\n',
    '[jobs]\napply_budget_usd = nan\n',
])
def test_jobs_invalid(tmp_path: Path, extra):
    _write(tmp_path, extra)
    with pytest.raises(ValueError, match="jobs"):
        load_config(tmp_path)

# --- [jobs] jobagent keys ---

def test_jobs_jobagent_defaults(tmp_path: Path):
    _write(tmp_path)
    cfg = load_config(tmp_path)
    j = cfg.jobs
    assert j.jobagent_dir == "~/Desktop/side_project/job_finding_agent"
    assert j.jobagent_profile == "config/profile.yaml"
    assert j.jobagent_model is None
    assert j.notify_only_companies == ("Google", "Amazon", "Meta", "Apple", "Microsoft", "Nvidia", "OpenAI")
    assert j.human_timeout_minutes == 30 and j.summary_mail_wait_minutes == 20
    assert cfg.jobagent_dir == Path.home() / "Desktop/side_project/job_finding_agent"
    assert cfg.jobagent_live_dir == cfg.jobagent_dir / "data" / "live"

def test_jobs_jobagent_keys_parsed(tmp_path: Path):
    _write(tmp_path, '[jobs]\njobagent_dir = "/abs/ja"\njobagent_profile = "cfg/p.yaml"\n'
                     'jobagent_model = "claude-x"\nnotify_only_companies = ["Acme", " Initech "]\n'
                     'human_timeout_minutes = 45\nsummary_mail_wait_minutes = 5\n')
    cfg = load_config(tmp_path)
    j = cfg.jobs
    assert j.jobagent_dir == "/abs/ja" and j.jobagent_profile == "cfg/p.yaml" and j.jobagent_model == "claude-x"
    assert j.notify_only_companies == ("Acme", "Initech")
    assert j.human_timeout_minutes == 45 and j.summary_mail_wait_minutes == 5
    assert cfg.jobagent_dir == Path("/abs/ja")
    assert cfg.jobagent_live_dir == Path("/abs/ja/data/live")

def test_jobs_jobagent_dir_relative_to_root(tmp_path: Path):
    _write(tmp_path, '[jobs]\njobagent_dir = "ja"\n')
    assert load_config(tmp_path).jobagent_dir == tmp_path / "ja"

def test_jobs_notify_only_empty_list_allowed(tmp_path: Path):
    _write(tmp_path, '[jobs]\nnotify_only_companies = []\n')
    assert load_config(tmp_path).jobs.notify_only_companies == ()

@pytest.mark.parametrize("extra, key", [
    ('jobagent_dir = ""', "jobagent_dir"),
    ('jobagent_profile = 3', "jobagent_profile"),
    ('jobagent_model = ""', "jobagent_model"),
    ('jobagent_model = 1', "jobagent_model"),
    ('notify_only_companies = "Google"', "notify_only_companies"),
    ('notify_only_companies = ["Google", ""]', "notify_only_companies"),
    ('notify_only_companies = ["Google", 3]', "notify_only_companies"),
    ('human_timeout_minutes = 0', "human_timeout_minutes"),
    ('human_timeout_minutes = 1.5', "human_timeout_minutes"),
    ('human_timeout_minutes = true', "human_timeout_minutes"),
    ('summary_mail_wait_minutes = -1', "summary_mail_wait_minutes"),
    ('summary_mail_wait_minutes = "20"', "summary_mail_wait_minutes"),
])
def test_jobs_jobagent_invalid(tmp_path: Path, extra, key):
    _write(tmp_path, f'[jobs]\n{extra}\n')
    with pytest.raises(ValueError, match=f"jobs\\.{key}"):
        load_config(tmp_path)
