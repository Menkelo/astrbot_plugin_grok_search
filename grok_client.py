from __future__ import annotations

import asyncio
import base64
import json
import os
import re
from typing import Any, List, Optional, Tuple

import aiohttp


# grok2api 通过请求体 tools 数组声明服务端搜索工具：
#   [{"type": "web_search"}]      -> 开启联网搜索
#   [{"type": "x_search"}]        -> 开启 X（推特）搜索
# 两者可同时声明，由模型按需调用。参考 chenyme/grok2api 的转换层。

GROK2API_SEARCH_WEB = "web"
GROK2API_SEARCH_X = "x"
GROK2API_SEARCH_ALL = "all"

# 单张图片（本地/base64）允许的最大字节数，超过则跳过
MAX_IMAGE_BYTES = 8 * 1024 * 1024


def normalize_base_url(url: str) -> str:
    """归一化 grok2api 地址：补协议、去尾部斜杠；未以 /v1 结尾时自动追加。"""
    u = (url or "").strip().rstrip("/")
    if not u:
        return ""
    if not u.startswith(("http://", "https://")):
        u = "http://" + u
    if not u.endswith("/v1"):
        u += "/v1"
    return u


def build_search_tools(kinds: List[str]) -> List[dict]:
    """根据搜索类型构建 grok2api 的服务端工具声明。"""
    tools: List[dict] = []
    if GROK2API_SEARCH_WEB in kinds or GROK2API_SEARCH_ALL in kinds:
        tools.append({"type": "web_search"})
    if GROK2API_SEARCH_X in kinds or GROK2API_SEARCH_ALL in kinds:
        tools.append({"type": "x_search"})
    return tools


def sniff_image_mime(data: bytes) -> str:
    """按魔数识别常见图片格式。"""
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data[:2] == b"BM":
        return "image/bmp"
    return "image/jpeg"


def image_spec_to_url(spec: str) -> Optional[str]:
    """把图片规格（http(s)/data URI/base64://本地路径）转换为 chat completions 可用的 image_url。
    本地文件与 base64 统一转为 data URI；无法处理时返回 None。"""
    if not isinstance(spec, str) or not spec.strip():
        return None
    s = spec.strip()
    ls = s.lower()
    if ls.startswith(("http://", "https://", "data:image/")):
        return s
    try:
        if ls.startswith("base64://"):
            raw = base64.b64decode(s[len("base64://"):] + "===", validate=False)
            if not raw or len(raw) > MAX_IMAGE_BYTES:
                return None
            mime = sniff_image_mime(raw)
            return f"data:{mime};base64,{base64.b64encode(raw).decode('ascii')}"
        fp = s
        if ls.startswith("file://"):
            fp = s[7:]
            # /C:/path -> C:/path
            if fp.startswith("/") and len(fp) > 3 and fp[2] == ":":
                fp = fp[1:]
        if os.path.isfile(fp):
            with open(fp, "rb") as f:
                raw = f.read(MAX_IMAGE_BYTES + 1)
            if not raw or len(raw) > MAX_IMAGE_BYTES:
                return None
            mime = sniff_image_mime(raw)
            return f"data:{mime};base64,{base64.b64encode(raw).decode('ascii')}"
    except Exception:
        return None
    return None


def extract_citations(payload: dict) -> List[Tuple[str, str]]:
    """从 chat completions 响应中提取引用链接（annotations.url_citation），按 URL 去重。"""
    citations: List[Tuple[str, str]] = []
    seen = set()
    try:
        choices = payload.get("choices") or []
        message = (choices[0] or {}).get("message") or {}
        annotations = message.get("annotations") or []
        if not isinstance(annotations, list):
            return citations
        for ann in annotations:
            if not isinstance(ann, dict):
                continue
            entry = ann.get("url_citation") if isinstance(ann.get("url_citation"), dict) else ann
            url = str(entry.get("url") or "").strip()
            if not url or url in seen:
                continue
            seen.add(url)
            title = str(entry.get("title") or "").strip()
            citations.append((title, url))
    except Exception:
        pass
    return citations


def extract_reply_text(payload: dict) -> str:
    """提取 chat completions 响应正文。"""
    try:
        choices = payload.get("choices") or []
        message = (choices[0] or {}).get("message") or {}
        content = message.get("content")
        if isinstance(content, str):
            return content.strip()
        if isinstance(content, list):
            parts = [
                str(p.get("text") or "")
                for p in content
                if isinstance(p, dict) and p.get("type") in ("text", "output_text")
            ]
            return "\n".join(x for x in parts if x).strip()
    except Exception:
        pass
    return ""


def format_citations(citations: List[Tuple[str, str]]) -> str:
    """把引用链接格式化为追加在回复尾部的参考列表。"""
    lines = []
    for i, (title, url) in enumerate(citations, 1):
        label = title if title else url
        lines.append(f"[{i}] {label}（{url}）")
    if not lines:
        return ""
    return "参考链接：\n" + "\n".join(lines)


class GrokSearchError(Exception):
    """grok2api 调用失败（网络/鉴权/上游错误）。"""


class GrokChatClient:
    """直连 grok2api 的 OpenAI 兼容端点：普通对话（含图片）、联网搜索、X 搜索。"""

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str,
        timeout_sec: int = 90,
        retry_times: int = 2,
        logger: Optional[Any] = None,
    ):
        self._endpoint = normalize_base_url(base_url) + "/chat/completions"
        self._api_key = (api_key or "").strip()
        self._model = (model or "").strip()
        self._timeout_sec = max(5, int(timeout_sec))
        self._retry_times = max(1, int(retry_times))
        self._logger = logger

    @property
    def ready(self) -> bool:
        return bool(self._endpoint) and bool(self._api_key)

    async def chat(
        self,
        *,
        user_prompt: str,
        system_prompt: str,
        image_specs: Optional[List[str]] = None,
        kinds: Optional[List[str]] = None,
    ) -> Tuple[str, List[Tuple[str, str]]]:
        """发起一次对话。可带图片（视觉）与搜索工具，返回 (正文, [(标题, URL), ...])。"""
        user_content: Any = user_prompt
        if image_specs:
            parts: List[dict] = [{"type": "text", "text": user_prompt}]
            for spec in image_specs:
                url = image_spec_to_url(spec)
                if url:
                    parts.append({"type": "image_url", "image_url": {"url": url}})
                elif self._logger is not None:
                    self._logger.warning("grok_search: skip unsupported image spec: %s", str(spec)[:120])
            if len(parts) > 1:
                user_content = parts

        body: dict = {
            "model": self._model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_content},
            ],
            "stream": False,
        }
        tools = build_search_tools(kinds or [])
        if tools:
            body["tools"] = tools

        last_exc: Optional[Exception] = None
        for i in range(self._retry_times):
            try:
                return await asyncio.wait_for(self._request_once(body), timeout=self._timeout_sec)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                last_exc = e
                if i >= self._retry_times - 1:
                    break
                await asyncio.sleep(min(0.5 * (2 ** i), 3.0))

        raise GrokSearchError(str(last_exc))

    async def _request_once(self, body: dict) -> Tuple[str, List[Tuple[str, str]]]:
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }
        async with aiohttp.ClientSession() as session:
            async with session.post(self._endpoint, json=body, headers=headers) as resp:
                text = await resp.text()
                if resp.status != 200:
                    if self._logger is not None:
                        self._logger.warning(
                            "grok_search: grok2api request failed, status=%s body=%s",
                            resp.status,
                            text[:300],
                        )
                    raise GrokSearchError(f"grok2api HTTP {resp.status}: {text[:200]}")
        try:
            payload = json.loads(text)
        except ValueError as e:
            raise GrokSearchError(f"grok2api 响应非 JSON: {e}") from e

        reply = extract_reply_text(payload)
        if not reply:
            # 可能是上游报错被包装成了 200，或正文为空
            err_msg = ""
            try:
                err_msg = str((payload.get("error") or {}).get("message") or "")[:200]
            except Exception:
                pass
            raise GrokSearchError(f"grok2api 返回空正文{'：' + err_msg if err_msg else ''}")
        return reply, extract_citations(payload)
