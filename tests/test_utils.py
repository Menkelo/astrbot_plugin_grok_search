from __future__ import annotations

import unittest

from astrbot_plugin_grok_search.main import GrokSearchPlugin
from astrbot_plugin_grok_search.grok_client import (
    build_search_tools,
    extract_citations,
    extract_reply_text,
    format_citations,
    normalize_base_url,
)
from astrbot_plugin_grok_search.formatter import demote_markdown_to_text, normalize_link_spacing


class TestQueryExtraction(unittest.TestCase):
    def test_web_query(self):
        x = lambda t: GrokSearchPlugin.extract_query(t, "web")
        self.assertEqual(x("/search 今天天气"), "今天天气")
        self.assertEqual(x("search 今天天气"), "今天天气")
        self.assertEqual(x("/搜索 今天天气"), "今天天气")
        self.assertEqual(x("/搜索：今天天气"), "今天天气")
        self.assertEqual(x("/联网搜索 北京新闻"), "北京新闻")
        self.assertEqual(x("/search 一下今天天气"), "今天天气")
        self.assertEqual(x("/search"), "")
        self.assertEqual(x(""), "")

    def test_x_query(self):
        x = lambda t: GrokSearchPlugin.extract_query(t, "x")
        self.assertEqual(x("/xsearch 马斯克最新动态"), "马斯克最新动态")
        self.assertEqual(x("/x搜索 马斯克"), "马斯克")
        self.assertEqual(x("/推特搜索 世界杯"), "世界杯")
        self.assertEqual(x("/搜推特 AI"), "AI")
        self.assertEqual(x("xsearch 马斯克"), "马斯克")
        self.assertEqual(x("/xsearch"), "")

    def test_multiline_query(self):
        q = GrokSearchPlugin.extract_query("/search 今天的天气\n要详细的", "web")
        self.assertEqual(q, "今天的天气\n要详细的")


class TestGrokClientPure(unittest.TestCase):
    def test_normalize_base_url(self):
        self.assertEqual(normalize_base_url("http://127.0.0.1:8000"), "http://127.0.0.1:8000/v1")
        self.assertEqual(normalize_base_url("http://127.0.0.1:8000/"), "http://127.0.0.1:8000/v1")
        self.assertEqual(normalize_base_url("http://127.0.0.1:8000/v1"), "http://127.0.0.1:8000/v1")
        self.assertEqual(normalize_base_url("http://127.0.0.1:8000/v1/"), "http://127.0.0.1:8000/v1")
        self.assertEqual(normalize_base_url("127.0.0.1:8000"), "http://127.0.0.1:8000/v1")
        self.assertEqual(normalize_base_url("  "), "")

    def test_build_search_tools(self):
        self.assertEqual(build_search_tools(["web"]), [{"type": "web_search"}])
        self.assertEqual(build_search_tools(["x"]), [{"type": "x_search"}])
        self.assertEqual(
            build_search_tools(["x"], x_from_date="2026-08-01", x_to_date="2026-08-24"),
            [{"type": "x_search", "from_date": "2026-08-01", "to_date": "2026-08-24"}],
        )
        self.assertEqual(
            build_search_tools(["x"], x_from_date="2026/08/01", x_to_date="bad"),
            [{"type": "x_search"}],
        )
        self.assertEqual(build_search_tools([]), [])

    def test_extract_response(self):
        payload = {
            "choices": [
                {
                    "message": {
                        "content": "今天北京晴 [1]。",
                        "annotations": [
                            {"type": "url_citation", "url_citation": {"title": "天气网", "url": "https://a.com/1"}},
                            {"type": "url_citation", "url_citation": {"title": "重复", "url": "https://a.com/1"}},
                            {"type": "url_citation", "url_citation": {"title": "新闻", "url": "https://b.com/2"}},
                        ],
                    }
                }
            ]
        }
        self.assertEqual(extract_reply_text(payload), "今天北京晴 [1]。")
        self.assertEqual(
            extract_citations(payload),
            [("天气网", "https://a.com/1"), ("新闻", "https://b.com/2")],
        )
        formatted = format_citations(extract_citations(payload))
        self.assertIn("[1] 天气网（https://a.com/1）", formatted)
        self.assertIn("[2] 新闻（https://b.com/2）", formatted)

        payload_parts = {
            "choices": [
                {"message": {"content": [{"type": "output_text", "text": "你好"}, {"type": "text", "text": "世界"}]}}
            ]
        }
        self.assertEqual(extract_reply_text(payload_parts), "你好\n世界")
        self.assertEqual(extract_reply_text({}), "")
        self.assertEqual(extract_citations({}), [])


class TestFormatter(unittest.TestCase):
    def test_demote_markdown(self):
        s = lambda t: demote_markdown_to_text(normalize_link_spacing(t))
        self.assertEqual(
            s("天气[**晴**](https://a.com)[[2]](https://b.com)"),
            "天气晴（https://a.com） [2] https://b.com",
        )
        self.assertEqual(s("## 标题\n正文 `code` ~~删~~ *斜* **粗**"), "标题\n正文 code 删 斜 粗")


if __name__ == "__main__":
    unittest.main()
