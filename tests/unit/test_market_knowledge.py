"""Issue #34：行情库纯函数（HTML 解析/分类/图片规范化/参考块格式化）。"""

from __future__ import annotations

import io

from app.services.market_knowledge import (
    _normalize_image,
    article_category,
    format_reference_block,
    parse_article_html,
)


def test_parse_article_html_extracts_title_text_and_lazy_images():
    html = """
    <html><head><title>文章标题</title></head><body>
      <div id="js_content">
        <p>第一段正文</p>
        <li>第二段：<span>嵌套文本只取一次</span></li>
        <img data-src="https://mmbiz.qpic.cn/mmbiz_jpg/a" />
      </div>
    </body></html>
    """
    parsed = parse_article_html(html, "https://mp.weixin.qq.com/s/x")
    assert parsed["title"] == "文章标题"
    assert "第一段正文" in parsed["text"]
    assert "第二段" in parsed["text"]
    assert parsed["images"] == ["https://mmbiz.qpic.cn/mmbiz_jpg/a"]


def test_article_category():
    assert article_category("url", "https://mp.weixin.qq.com/s/x") == "公众号文章"
    assert article_category("url", "https://example.com/a.html") == "网页文章"
    assert article_category("upload", "tips.pdf") == "文档"
    assert article_category("upload", "photo.png") == "图片"


def test_normalize_image_webp_to_png():
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGBA", (8, 8), (1, 2, 3, 255)).save(buf, format="WEBP")
    data, name = _normalize_image(buf.getvalue(), 3)
    assert name == "图片3.png"
    assert data[:8] == b"\x89PNG\r\n\x1a\n"


def test_format_reference_block_empty_and_capped():
    assert format_reference_block([]) == "（行情库暂无已提炼要点）"
    block = format_reference_block([{"content": "要点内容"}])
    assert "低优先级辅助参考" in block
    assert "要点内容" in block
