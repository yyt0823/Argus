from argus.tools.history import format_history

def test_format():
    rows = [{"id": 2, "ts": "2026-10-01T09:01:00", "sender": "argus", "content": "a\nb"},
            {"id": 1, "ts": "2026-10-01T09:00:00", "sender": "user", "content": "hi"}]
    assert format_history(rows) == "[2026-10-01T09:01:00] argus: a / b\n[2026-10-01T09:00:00] user: hi"

def test_empty():
    assert format_history([]) == "No matching messages."
