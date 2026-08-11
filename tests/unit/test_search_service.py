from __future__ import annotations

from app.services.search_service import sanitize_query, trust_level


def test_sanitize_query_masks_pii():
    dirty = "联系人 13812345678，证件 110101199001011234，卡 6222021234567890"
    clean = sanitize_query(dirty)
    assert "13812345678" not in clean
    assert "110101199001011234" not in clean
    assert "6222021234567890" not in clean
    assert "[电话]" in clean and "[证件号]" in clean and "[银行卡]" in clean


def test_trust_level_domains():
    assert trust_level("https://www.gov.cn/x") == 1
    assert trust_level("https://news.example.com/x") == 2
    assert trust_level("https://zhihu.com/question/1") == 3
