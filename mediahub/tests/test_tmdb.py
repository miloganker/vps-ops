from app.tmdb import Candidate, key_type, score_candidates


def test_key_type():
    assert key_type("eyJhbGciOiJIUzI1NiJ9.x.y") == "v4"
    assert key_type("0123456789abcdef0123456789abcdef") == "v3"


def test_reply_1988_ambiguous_is_medium():
    c = [Candidate(64010, "请回答1988", "응답하라 1988", 2015, 30, en_title="Reply 1988"),
         Candidate(999, "请回答1988（国语版）", "응답하라 1988", 2015, 3, en_title="Reply 1988")]
    m = score_candidates(c, "Reply 1988", "请回答1988", 2015)
    assert m.best.tmdb_id == 64010
    assert m.confidence == "medium"


def test_unique_exact_high():
    c = [Candidate(11645, "乱", "乱", 1985, 10, en_title="Ran"),
         Candidate(1, "Dhee Rani", "Dhee Rani", 1985, 1, en_title="Dhee Rani")]
    m = score_candidates(c, "Ran", "", 1985)
    assert (m.best.tmdb_id, m.confidence) == (11645, "high")


def test_no_result():
    assert score_candidates([], "x", "", None).confidence == "none"


def test_no_year_single_name_is_not_high():
    c = [Candidate(5, "新建文件夹", "新建文件夹", 2014, 1)]
    m = score_candidates(c, "", "新建文件夹", None)
    assert m.confidence == "medium"


def test_no_year_both_names_high():
    c = [Candidate(1396, "绝命毒师", "Breaking Bad", 2008, 100, en_title="Breaking Bad"),
         Candidate(2, "绝命卦师", "绝命卦师", 2016, 5)]
    m = score_candidates(c, "Breaking Bad", "绝命毒师", None)
    assert (m.best.tmdb_id, m.confidence) == (1396, "high")
