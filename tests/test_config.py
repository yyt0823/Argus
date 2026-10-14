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
