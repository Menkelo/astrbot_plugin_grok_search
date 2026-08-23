from __future__ import annotations

import base64
import os
import struct
import tempfile
import unittest

from astrbot_plugin_grok_search.main import ZssmGrokPlugin
from astrbot_plugin_grok_search.grok_client import (
    build_search_tools,
    extract_citations,
    extract_reply_text,
    image_spec_to_url,
    normalize_base_url,
    parse_models_payload,
    sniff_image_mime,
)
from astrbot_plugin_grok_search.formatter import demote_markdown_to_text, normalize_link_spacing
from astrbot_plugin_grok_search.file_preview_utils import build_text_exts_from_config


def _tiny_png_bytes() -> bytes:
    # 1x1 红色 PNG
    return base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
    )


class TestZssmTrigger(unittest.TestCase):
    def test_strip_trigger_content(self):
        self.assertEqual(ZssmGrokPlugin._strip_trigger_and_get_content("zssm hello"), "hello")
        self.assertEqual(ZssmGrokPlugin._strip_trigger_and_get_content("  /zssm  hello  "), "hello")
        self.assertEqual(ZssmGrokPlugin._strip_trigger_and_get_content("zssm? hello"), "hello")
        self.assertEqual(ZssmGrokPlugin._strip_trigger_and_get_content("zssm：hello"), "hello")
        self.assertEqual(ZssmGrokPlugin._strip_trigger_and_get_content("zssm，hello world"), "hello world")
        self.assertEqual(ZssmGrokPlugin._strip_trigger_and_get_content("zssm"), "")

    def test_is_zssm_trigger(self):
        self.assertTrue(ZssmGrokPlugin._is_zssm_trigger("zssm hello"))
        self.assertTrue(ZssmGrokPlugin._is_zssm_trigger("zssm：什么是量子计算"))
        self.assertFalse(ZssmGrokPlugin._is_zssm_trigger("hello world"))
        self.assertFalse(ZssmGrokPlugin._is_zssm_trigger("zssmhello"))

    def test_decide_search_kind(self):
        k = ZssmGrokPlugin._decide_search_kind
        self.assertEqual(k("搜索一下今天的天气"), "web")
        self.assertEqual(k("联网搜索 北京天气"), "web")
        self.assertEqual(k("帮我查一下上海到北京的高铁"), "web")
        self.assertEqual(k("x搜索 马斯克最新动态"), "x")
        self.assertEqual(k("推特搜索 世界杯"), "x")
        self.assertEqual(k("搜一下推特上关于AI的讨论"), "x")
        self.assertEqual(k("全搜 今天大事"), "all")
        self.assertEqual(k("全网搜索：科技新闻"), "all")
        self.assertEqual(k("混合搜索 火箭"), "all")
        self.assertEqual(k("什么是量子计算"), "")
        self.assertEqual(k(""), "")
        # 不应误判
        self.assertEqual(k("查xlsx怎么用"), "")
        self.assertEqual(k("推荐一下好吃的"), "")

    def test_extract_search_query(self):
        q = ZssmGrokPlugin._extract_search_query
        self.assertEqual(q("搜索一下今天的天气"), "今天的天气")
        self.assertEqual(q("x搜索 马斯克最新动态"), "马斯克最新动态")
        self.assertEqual(q("全搜 今天大事"), "今天大事")
        self.assertEqual(q("非搜索文本"), "非搜索文本")


class TestFormatter(unittest.TestCase):
    def test_demote_keeps_section_headers(self):
        s = lambda t: demote_markdown_to_text(normalize_link_spacing(t))
        self.assertEqual(
            s("**关键词**\n天气 [[1]](https://a.com)[[2]](https://b.com) **详细阐述** 晴"),
            "**关键词**\n天气 [1] https://a.com [2] https://b.com **详细阐述** 晴",
        )

    def test_demote_markdown(self):
        s = lambda t: demote_markdown_to_text(normalize_link_spacing(t))
        self.assertEqual(s("## 标题\n正文 `code` ~~删~~ *斜* **粗**"), "标题\n正文 code 删 斜 粗")


class TestGrokClientPure(unittest.TestCase):
    def test_normalize_base_url(self):
        self.assertEqual(normalize_base_url("http://127.0.0.1:8000"), "http://127.0.0.1:8000/v1")
        self.assertEqual(normalize_base_url("http://127.0.0.1:8000/v1/"), "http://127.0.0.1:8000/v1")
        self.assertEqual(normalize_base_url("127.0.0.1:8000"), "http://127.0.0.1:8000/v1")
        self.assertEqual(normalize_base_url("  "), "")

    def test_build_search_tools(self):
        self.assertEqual(build_search_tools(["web"]), [{"type": "web_search"}])
        self.assertEqual(build_search_tools(["x"]), [{"type": "x_search"}])
        self.assertEqual(
            build_search_tools(["all"]),
            [{"type": "web_search"}, {"type": "x_search"}],
        )
        self.assertEqual(build_search_tools([]), [])

    def test_image_spec_to_url(self):
        # data URI 原样保留；http 交给下载逻辑（此处返回 None）
        self.assertEqual(
            image_spec_to_url("data:image/png;base64,AAA"),
            "data:image/png;base64,AAA",
        )
        self.assertIsNone(image_spec_to_url("https://a.com/x.png"))
        # base64:// 转 data URI 并嗅探 mime
        png = _tiny_png_bytes()
        uri = image_spec_to_url("base64://" + base64.b64encode(png).decode("ascii"))
        self.assertTrue(uri.startswith("data:image/png;base64,"))
        # 本地文件转 data URI
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
            f.write(png)
            path = f.name
        try:
            uri2 = image_spec_to_url(path)
            self.assertTrue(uri2.startswith("data:image/png;base64,"))
        finally:
            os.remove(path)
        # 非图片/不存在 -> None
        self.assertIsNone(image_spec_to_url("Z:\\not\\exist.png"))
        self.assertIsNone(image_spec_to_url(""))

    def test_sniff_image_mime(self):
        self.assertEqual(sniff_image_mime(_tiny_png_bytes()), "image/png")
        self.assertEqual(sniff_image_mime(b"\xff\xd8\xff\xe0abc"), "image/jpeg")
        self.assertEqual(sniff_image_mime(b"GIF89a1234"), "image/gif")

    def test_extract_response(self):
        payload = {
            "choices": [
                {
                    "message": {
                        "content": "今天北京晴 [1]。",
                        "annotations": [
                            {"type": "url_citation", "url_citation": {"title": "天气网", "url": "https://a.com/1"}},
                            {"type": "url_citation", "url_citation": {"title": "重复", "url": "https://a.com/1"}},
                        ],
                    }
                }
            ]
        }
        self.assertEqual(extract_reply_text(payload), "今天北京晴 [1]。")
        self.assertEqual(extract_citations(payload), [("天气网", "https://a.com/1")])
        self.assertEqual(extract_reply_text({}), "")
        self.assertEqual(extract_citations({}), [])

    def test_parse_models_payload(self):
        payload = {
            "object": "list",
            "data": [
                {"id": "grok-4", "object": "model"},
                {"id": "grok-4-fast", "object": "model"},
                {"id": "grok-4"},  # 重复去重
                {"object": "model"},  # 无 id 跳过
                "bad-item",  # 非对象跳过
            ],
        }
        self.assertEqual(parse_models_payload(payload), ["grok-4", "grok-4-fast"])
        self.assertEqual(parse_models_payload({}), [])
        self.assertEqual(parse_models_payload({"data": None}), [])


class TestFileExts(unittest.TestCase):
    def test_build_text_exts(self):
        exts = build_text_exts_from_config("md, json, .py", ["txt"])
        self.assertIn(".txt", exts)
        self.assertIn(".md", exts)
        self.assertIn(".json", exts)
        self.assertIn(".py", exts)


if __name__ == "__main__":
    unittest.main()
