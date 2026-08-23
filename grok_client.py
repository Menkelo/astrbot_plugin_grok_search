from __future__ import annotations

import asyncio
import json
import re
from typing import Any, List, Optional, Tuple

import aiohttp


# grok2api 通过请求体 tools 数组声明服务端搜索工具：
#   [{"type": "web_search"}]                                   -> 开启联网搜索
#   [{"type": "x_search", "from_date": "...", "to_date": "..."}] -> 开启 X（推特）搜索
# 参考 chenyme/grok2api 的 chat completions -> Responses 转换层。

GROK2API_SEARCH_WEB = "web"
GROK2API_SEARCH_X = "x"

DATE_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}$")


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


def build_search_tools(
    kinds: List[str],
    *,
    x_from_date: str = "",
    x_to_date: str = "",
) -> List[dict]:
    """根据搜索类型构建 grok2api 的服务端工具声明。"""
    tools: List[dict] = []
    if GROK2API_SEARCH_WEB in kinds:
        tools.append({"type": "web_search"})
    if GROK2API_SEARCH_X in kinds:
        tool: dict = {"type": "x_search"}
        for field, value in (("from_date", x_from_date), ("to_date", x_to_date)):
            v = (value or "").strip()
            if v and DATE_PATTERN.match(v):
                tool[field] = v
        tools.append(tool)
    return tools


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


class GrokSearchClient:
    """直连 grok2api 的 OpenAI 兼容端点，通过服务端工具开启联网搜索 / X 搜索。"""

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

    async def search(
        self,
        *,
        user_prompt: str,
        system_prompt: str,
        kinds: List[str],
        x_from_date: str = "",
        x_to_date: str = "",
    ) -> Tuple[str, List[Tuple[str, str]]]:
        """发起带搜索工具的对话，返回 (正文, [(标题, URL), ...])。"""
        tools = build_search_tools(kinds, x_from_date=x_from_date, x_to_date=x_to_date)
        if not tools:
            raise GrokSearchError("no search tool enabled")

        body = {
            "model": self._model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "tools": tools,
            "stream": False,
        }

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
