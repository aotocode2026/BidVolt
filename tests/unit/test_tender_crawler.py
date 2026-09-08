"""Issue #32：附件发现与分类（纯函数，无浏览器依赖）。"""

from __future__ import annotations

from app.services.tender_crawler import (
    extract_actions,
    extract_links,
    parse_content_disposition,
)


def test_extract_links_skips_navigation_and_picks_files():
    base = "https://sgccetp.com.cn/portal/#/doc/x"
    links = extract_links(
        base,
        [
            ("招标公告", "https://sgccetp.com.cn/portal/#/list/list-spe/1"),
            ("下载公告文件", "javascript:void(0)"),
            ("下载", "https://sgccetp.com.cn/files/a.pdf"),
            ("附件", "https://sgccetp.com.cn/files/b.zip"),
            ("联系方式", "https://sgccetp.com.cn/portal/#/doc/contact"),
        ],
    )
    assert [item.href for item in links] == [
        "https://sgccetp.com.cn/files/a.pdf",
        "https://sgccetp.com.cn/files/b.zip",
    ]


def test_extract_actions_classifies_void_download_buttons():
    actions = extract_actions(
        "https://sgccetp.com.cn/portal/#/doc/x",
        [
            ("下载公告文件", "javascript:void(0);"),
            ("获取招标文件", "javascript:void(0);"),
        ],
    )
    assert [(a.text, a.kind, a.is_action, a.login_candidate) for a in actions] == [
        ("下载公告文件", "公告附件", True, False),
        ("获取招标文件", "招标文件", True, True),
    ]


def test_parse_content_disposition():
    assert (
        parse_content_disposition("attachment; filename*=UTF-8''%E6%8B%9B%E6%A0%87%E5%85%AC%E5%91%8A.zip")
        == "招标公告.zip"
    )
    assert parse_content_disposition('attachment; filename="a.pdf"') == "a.pdf"
    assert parse_content_disposition(None) is None
