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
