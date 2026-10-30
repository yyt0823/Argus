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
    posting = {"company": "Initrode Corp", "role": "Data Intern (R482913)", "url": "https://a.com/1"}
    rows = [_row(3, "Initrode", "Analyst Co-op", url="https://b.com/2", req="r482913")]
    assert check_duplicate(posting, rows, [], []).kind == "duplicate"


def test_dedup_different_req_ids_same_role_is_none():
    posting = {"company": "Initrode", "role": "Data Science Intern R482914", "url": "https://a.com/n"}
    rows = [
        _row(1, "Initrode", "Data Science Intern", req="R482913"),
        _row(2, "Initrode", "Data Science Intern", url="https://a.com/R482915"),
    ]
    assert check_duplicate(posting, rows, [], []).kind == "none"


def test_dedup_similar_role_without_req_is_possible():
    posting = {"company": "Pied Piper", "role": "Backend Developer Intern Summer 2027", "url": "https://c.com/1"}
    rows = [_row(12, "Pied Piper", "Backend Developer Intern")]
    v = check_duplicate(posting, rows, [], [])
    assert v.kind == "possible"
    assert v.detail == "tracker #12 Pied Piper — Backend Developer Intern"


def test_dedup_other_company_sharing_word_is_none():
    posting = {"company": "Vandelay Bank", "role": "Software Engineer", "url": "https://a.com/1"}
    rows = [_row(1, "Bank Inc", "Software Engineer")]
    assert check_duplicate(posting, rows, [], []).kind == "none"


def test_dedup_duplicate_later_beats_possible_earlier():
    posting = {"company": "Pied Piper", "role": "Backend Developer Intern", "url": "https://c.com/9"}
    rows = [_row(1, "Pied Piper", "Backend Developer Intern II")]
    subs = [{"id": 31, "company": "Pied Piper", "role": "x", "url": "https://c.com/9"}]
    v = check_duplicate(posting, rows, subs, [])
    assert v.kind == "duplicate" and v.detail.startswith("posting #31")


def test_dedup_jobagent_only_counts_submitted_or_already_applied():
    posting = {"company": "Contoso", "role": "Tech Intern", "url": "https://c.com/1"}
    entry = {"at": "2026-10-23T10:00:00", "job_url": "https://c.com/1", "company": "Contoso", "job_title": "Tech Intern"}
    assert check_duplicate(posting, [], [], [{**entry, "status": "failed"}]).kind == "none"
    v = check_duplicate(posting, [], [], [{**entry, "status": "already_applied"}])
    assert v == DupVerdict("duplicate", "jobagent 2026-10-23 Contoso — Tech Intern")


def test_helpers():
    assert norm_company("Acme Technologies Inc. (Canada)") == "acme"
    assert req_id("Role (123456)") == "123456"
    assert req_id("https://x.com/job_654321") == "654321"
    assert req_id("Engineer JR1234") == "JR1234"
    assert req_id("Engineer") is None
    assert role_tokens("Summer 2027 Data Intern") == {"data"}
    assert jaccard(set(), set()) == 0.0


WD = "https://acme.wd3.myworkdayjobs.com/{loc}External/job/Montreal/Data-Intern_{rid}"


def test_req_id_after_underscore_in_workday_url():
    assert req_id("https://x.com/job/Data-Intern_R482913-1") == "R482913"
    assert req_id("https://x.com/job/Dev_JR1234") == "JR1234"
    assert req_id("https://x.com/job/Dev_REQ-99") == "REQ-99"


def test_dedup_workday_url_with_and_without_locale():
    posting = {"company": "Acme", "role": "A", "url": WD.format(loc="en-US/", rid="R482913-1")}
    rows = [_row(1, "Zed", "B", url=WD.format(loc="", rid="R482913-1"))]
    assert check_duplicate(posting, rows, [], []).kind == "duplicate"
    assert norm_url("http://www.X.com/fr-CA/jobs/1/") == norm_url("https://x.com/jobs/1")
    assert norm_url("https://x.com/en/jobs") == norm_url("https://x.com/jobs")


def test_dedup_company_suffix_variants():
    assert norm_company("Initrode Financial Corporation") == norm_company("Initrode Financial")
    assert norm_company("Acme Co.") == norm_company("Acme Limited") == norm_company("ACME LLC")
    assert norm_company("Initrode Financial") != norm_company("Initrode")
    assert norm_company("Montréal Inc") == norm_company("Montreal")


def test_dedup_identical_raw_role_without_req_is_duplicate():
    posting = {"company": "Pied Piper", "role": "Backend Developer Intern", "url": "https://c.com/1"}
    rows = [_row(1, "Pied Piper", "backend  developer intern")]
    assert check_duplicate(posting, rows, [], []).kind == "duplicate"


def test_dedup_roles_equal_after_stopwords_only_is_possible():
    posting = {"company": "Pied Piper", "role": "Backend Developer Intern Winter 2027", "url": "https://c.com/1"}
    rows = [_row(1, "Pied Piper", "Backend Developer Intern Summer 2027")]
    assert check_duplicate(posting, rows, [], []).kind == "possible"


def test_dedup_req_id_formats_compare_by_digits():
    posting = {"company": "Initrode", "role": "Intern R482913", "url": "https://a.com/1"}
    rows = [_row(1, "Initrode", "Other", req="482913")]
    assert check_duplicate(posting, rows, [], []).kind == "duplicate"
    rows = [_row(1, "Initrode", "Intern", req="r482913"), _row(2, "Initrode", "Intern", req="482914")]
    assert check_duplicate(posting, rows, [], []).kind == "duplicate"


def test_dedup_uncertain_posting_same_url_is_only_possible():
    posting = {"company": "Acme", "role": "SWE Intern", "url": "https://jobs.example.com/p/42"}
    uncertain = [{"id": 9, "company": "Acme", "role": "SWE Intern", "url": "https://jobs.example.com/p/42"}]
    v = check_duplicate(posting, [], [], [], uncertain)
    assert v == DupVerdict("possible", "posting #9 (earlier attempt may have submitted)")


def test_dedup_uncertain_posting_similar_role_is_possible():
    posting = {"company": "Acme Inc", "role": "Backend Developer Intern", "url": "https://a.com/1"}
    uncertain = [{"id": 3, "company": "Acme", "role": "Backend Developer", "url": "https://a.com/2"}]
    assert check_duplicate(posting, [], [], [], uncertain).kind == "possible"


def test_dedup_uncertain_posting_unrelated_is_none():
    posting = {"company": "Acme", "role": "SWE Intern", "url": "https://a.com/1"}
    uncertain = [{"id": 3, "company": "Other", "role": "SWE Intern", "url": "https://b.com/2"}]
    assert check_duplicate(posting, [], [], [], uncertain).kind == "none"


def test_dedup_certain_duplicate_beats_uncertain_match():
    posting = {"company": "Acme", "role": "SWE Intern", "url": "https://a.com/1"}
    uncertain = [{"id": 3, "company": "Acme", "role": "SWE Intern", "url": "https://a.com/1"}]
    rows = [_row(5, "Acme", "SWE Intern", url="https://a.com/1")]
    assert check_duplicate(posting, rows, [], [], uncertain).kind == "duplicate"
