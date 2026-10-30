from argus.jobs.dedup import (
    DupVerdict,
    check_duplicate,
    jaccard,
    norm_company,
    norm_url,
    req_id,
    role_tokens,
)


def _row(id, company, role, url=None, req=None):
    return {"id": id, "company": company, "role": role, "url": url, "req_id": req}


def test_dedup_same_url_with_tracking_params():
    posting = {"company": "Acme", "role": "Dev", "url": "https://Jobs.Example.com/p/42/?utm_source=x#apply"}
    rows = [_row(7, "Other", "Thing", url="https://jobs.example.com/p/42")]
    v = check_duplicate(posting, rows, [], [])
    assert v.kind == "duplicate"
    assert v.detail == "tracker #7 Other — Thing"


def test_dedup_keeps_identifying_query_params():
    a = norm_url("https://x.com/jobs?gh_jid=1&utm_medium=z")
    b = norm_url("https://x.com/jobs?gh_jid=2")
    assert a != b


def test_dedup_same_req_id_different_url():
    posting = {"company": "Intact Corp", "role": "Data Intern (R155233)", "url": "https://a.com/1"}
    rows = [_row(3, "Intact", "Analyst Co-op", url="https://b.com/2", req="r155233")]
    assert check_duplicate(posting, rows, [], []).kind == "duplicate"


def test_dedup_different_req_ids_same_role_is_none():
    posting = {"company": "Intact", "role": "Data Science Intern R155234", "url": "https://a.com/n"}
    rows = [
        _row(1, "Intact", "Data Science Intern", req="R155233"),
        _row(2, "Intact", "Data Science Intern", url="https://a.com/R155235"),
    ]
    assert check_duplicate(posting, rows, [], []).kind == "none"


def test_dedup_similar_role_without_req_is_possible():
    posting = {"company": "Coveo", "role": "Backend Developer Intern Summer 2027", "url": "https://c.com/1"}
    rows = [_row(12, "Coveo", "Backend Developer Intern")]
    v = check_duplicate(posting, rows, [], [])
    assert v.kind == "possible"
    assert v.detail == "tracker #12 Coveo — Backend Developer Intern"


def test_dedup_other_company_sharing_word_is_none():
    posting = {"company": "Capital One", "role": "Software Engineer", "url": "https://a.com/1"}
    rows = [_row(1, "One Inc", "Software Engineer")]
    assert check_duplicate(posting, rows, [], []).kind == "none"


def test_dedup_duplicate_later_beats_possible_earlier():
    posting = {"company": "Coveo", "role": "Backend Developer Intern", "url": "https://c.com/9"}
    rows = [_row(1, "Coveo", "Backend Developer Intern II")]
    subs = [{"id": 31, "company": "Coveo", "role": "x", "url": "https://c.com/9"}]
    v = check_duplicate(posting, rows, subs, [])
    assert v.kind == "duplicate" and v.detail.startswith("posting #31")


def test_dedup_jobagent_only_counts_submitted_or_already_applied():
    posting = {"company": "CIBC", "role": "Tech Intern", "url": "https://c.com/1"}
    entry = {"at": "2026-10-23T10:00:00", "job_url": "https://c.com/1", "company": "CIBC", "job_title": "Tech Intern"}
    assert check_duplicate(posting, [], [], [{**entry, "status": "failed"}]).kind == "none"
    v = check_duplicate(posting, [], [], [{**entry, "status": "already_applied"}])
    assert v == DupVerdict("duplicate", "jobagent 2026-10-23 CIBC — Tech Intern")


def test_helpers():
    assert norm_company("Acme Technologies Inc. (Canada)") == "acme"
    assert req_id("Role (123456)") == "123456"
    assert req_id("https://x.com/job_654321") == "654321"
    assert req_id("Engineer JR1234") == "JR1234"
    assert req_id("Engineer") is None
    assert role_tokens("Summer 2027 Data Intern") == {"data"}
    assert jaccard(set(), set()) == 0.0
