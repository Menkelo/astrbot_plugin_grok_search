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
# 远程图片下载超时（秒）与并发数
IMAGE_DOWNLOAD_TIMEOUT_SEC = 15
IMAGE_DOWNLOAD_CONCURRENCY = 4
IMAGE_DOWNLOAD_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept": "image/*,*/*;q=0.8",
}


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
    """把图片规格（data URI/base64://本地路径）转换为 chat completions 可用的 image_url。
    本地文件与 base64 统一转为 data URI；无法处理时返回 None。
    注意：http(s) 远程图片不在此处理（服务端可能拉取失败），由 chat() 先下载转 data URI。"""
    if not isinstance(spec, str) or not spec.strip():
        return None
    s = spec.strip()
    ls = s.lower()
    if ls.startswith(("http://", "https://")):
        return None  # 远程图片交给 download_image_to_data_uri
    if ls.startswith("data:image/"):
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


async def download_image_to_data_uri(
    url: str,
    *,
    timeout_sec: int = IMAGE_DOWNLOAD_TIMEOUT_SEC,
    max_bytes: int = MAX_IMAGE_BYTES,
    logger: Optional[Any] = None,
) -> Optional[str]:
    """把远程图片下载到本地并转为 data URI。

    QQ 转发/引用消息里的图片是腾讯 CDN 链接，grok2api 服务端往往无法直接抓取
    （会报 invalid_image / cannot resolve Image URL），因此在插件侧先下载内联。"""
    if not isinstance(url, str) or not url.strip().lower().startswith(("http://", "https://")):
        return None
    try:
        timeout = aiohttp.ClientTimeout(total=max(5, timeout_sec))
        async with aiohttp.ClientSession(timeout=timeout, headers=IMAGE_DOWNLOAD_HEADERS) as session:
            async with session.get(url.strip()) as resp:
                if resp.status != 200:
                    if logger is not None:
                        logger.warning("grok_search: download image HTTP %s: %s", resp.status, url[:160])
                    return None
                data = await resp.content.read(max_bytes + 1)
        if not data or len(data) > max_bytes:
            if logger is not None:
                logger.warning("grok_search: image too large or empty, skip: %s", url[:160])
            return None
        mime = sniff_image_mime(data)
        return f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}"
    except Exception as e:
        if logger is not None:
            logger.warning("grok_search: download image failed, skip: %s (%s)", url[:160], e)
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
            image_urls = await self._resolve_image_specs(image_specs)
            if image_urls:
                parts: List[dict] = [{"type": "text", "text": user_prompt}]
                for url in image_urls:
                    parts.append({"type": "image_url", "image_url": {"url": url}})
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

    async def _resolve_image_specs(self, image_specs: List[str]) -> List[str]:
        """把图片规格列表解析为可直接发送的 image_url 列表。
        远程图片先下载转 data URI（避免服务端抓取失败导致整个请求 400）；
        下载失败或无法识别的图片自动跳过，不影响本次请求。"""
        remote: List[str] = []
        local: List[str] = []
        for spec in image_specs:
            if not isinstance(spec, str) or not spec.strip():
                continue
            s = spec.strip()
            if s.lower().startswith(("http://", "https://")):
                remote.append(s)
            else:
                u = image_spec_to_url(s)
                if u:
                    local.append(u)
                elif self._logger is not None:
                    self._logger.warning("grok_search: skip unsupported image spec: %s", s[:120])

        if not remote:
            return local

        sem = asyncio.Semaphore(IMAGE_DOWNLOAD_CONCURRENCY)

        async def _fetch(url: str) -> Optional[str]:
            async with sem:
                return await download_image_to_data_uri(url, logger=self._logger)

        results = await asyncio.gather(*[_fetch(u) for u in remote], return_exceptions=True)
        for url, res in zip(remote, results):
            if isinstance(res, str) and res:
                local.append(res)
            elif self._logger is not None:
                self._logger.warning("grok_search: 图片下载失败已跳过，仅按文本处理: %s", url[:160])
        return local

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
