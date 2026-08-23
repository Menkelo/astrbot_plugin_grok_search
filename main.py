from __future__ import annotations

import re
import time
from typing import Any, Dict, List, Optional

from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star
from astrbot.api import logger
import astrbot.api.message_components as Comp

from .grok_client import (
    GrokSearchClient,
    GrokSearchError,
    GROK2API_SEARCH_WEB,
    GROK2API_SEARCH_X,
    format_citations,
)
from .prompt_utils import build_search_user_prompt, build_search_system_prompt
from .formatter import normalize_link_spacing, demote_markdown_to_text

GROK2API_BASE_URL_KEY = "grok2api_base_url"
GROK2API_API_KEY_KEY = "grok2api_api_key"
GROK2API_MODEL_KEY = "grok2api_model"
GROK2API_X_FROM_DATE_KEY = "grok2api_x_from_date"
GROK2API_X_TO_DATE_KEY = "grok2api_x_to_date"
LLM_TIMEOUT_SEC_KEY = "llm_timeout_sec"
LLM_RETRY_TIMES_KEY = "llm_retry_times"
SHOW_COST_KEY = "show_cost"
DEFAULT_GROK2API_MODEL = "grok-4"
DEFAULT_LLM_TIMEOUT_SEC = 90
DEFAULT_LLM_RETRY_TIMES = 2

USAGE_HINT = (
    "用法：\n"
    "/search <问题> - 联网搜索（别名：/搜索、/联网搜索）\n"
    "/xsearch <问题> - X（推特）搜索（别名：/x搜索、/推特搜索、/搜推特）\n"
    "/gsearch <问题> - 组合搜索，同时联网+X（别名：/全搜、/全网搜索）\n"
    "也可以回复一条消息后再发指令，被回复内容会作为搜索上下文。"
)

# 指令头 -> 搜索词。允许 / 前缀、别名与分隔符；x 类别名长的在前，避免被短的吃掉
WEB_QUERY_PATTERN = re.compile(
    r"^[/\\]?\s*(?:search|联网搜索|搜索)\s*[:：,，]?\s*([\s\S]+)$", re.I
)
X_QUERY_PATTERN = re.compile(
    r"^[/\\]?\s*(?:xsearch|推特搜索|搜推特|x搜索|x(?=[^a-z0-9]|$))\s*[:：,，]?\s*([\s\S]+)$",
    re.I,
)
# 组合搜索：一次请求同时开启联网搜索 + X 搜索
ALL_QUERY_PATTERN = re.compile(
    r"^[/\\]?\s*(?:gsearch|allsearch|全网搜索|全搜|混合搜索)\s*[:：,，]?\s*([\s\S]+)$",
    re.I,
)
# 去掉搜索词开头的口语填充词，如“/搜索 一下今天天气”
QUERY_FILLER_PATTERN = re.compile(r"^(?:一下|看看|帮我|关于|相关)\s*[:：,，]?\s*")


class GrokSearchPlugin(Star):
    """通过 grok2api 的服务端工具提供联网搜索与 X（推特）搜索。"""

    def __init__(self, context: Context, config: Optional[Dict[str, Any]] = None):
        super().__init__(context)
        self.config: Dict[str, Any] = config if config is not None else {}

    # === 配置读取 ===

    def _get_conf_str(self, key: str, default: str = "") -> str:
        try:
            v = self.config.get(key)
            if isinstance(v, str):
                return v.strip()
        except Exception:
            pass
        return default

    def _get_conf_int(self, key: str, default: int, min_v: int = 1, max_v: int = 120000) -> int:
        try:
            v = self.config.get(key)
            if isinstance(v, int):
                return max(min(v, max_v), min_v)
            if isinstance(v, str) and v.strip().isdigit():
                return max(min(int(v.strip()), max_v), min_v)
        except Exception:
            pass
        return default

    def _get_conf_bool(self, key: str, default: bool) -> bool:
        try:
            v = self.config.get(key)
            if isinstance(v, bool):
                return v
            if isinstance(v, str):
                lv = v.strip().lower()
                if lv in ("1", "true", "yes", "on"):
                    return True
                if lv in ("0", "false", "no", "off"):
                    return False
        except Exception:
            pass
        return default

    def _build_client(self) -> Optional[GrokSearchClient]:
        base_url = self._get_conf_str(GROK2API_BASE_URL_KEY)
        api_key = self._get_conf_str(GROK2API_API_KEY_KEY)
        if not base_url or not api_key:
            return None
        model = self._get_conf_str(GROK2API_MODEL_KEY) or DEFAULT_GROK2API_MODEL
        return GrokSearchClient(
            base_url=base_url,
            api_key=api_key,
            model=model,
            timeout_sec=self._get_conf_int(LLM_TIMEOUT_SEC_KEY, DEFAULT_LLM_TIMEOUT_SEC, 5, 600),
            retry_times=self._get_conf_int(LLM_RETRY_TIMES_KEY, DEFAULT_LLM_RETRY_TIMES, 1, 5),
            logger=logger,
        )

    # === 消息解析 ===

    @staticmethod
    def _message_text(event: AstrMessageEvent) -> str:
        try:
            s = event.get_message_str()
        except Exception:
            s = getattr(event, "message_str", "") or ""
        return s.strip() if isinstance(s, str) else ""

    @staticmethod
    def extract_query(text: str, kinds: List[str]) -> str:
        """从消息文本中提取搜索词（去掉指令头与开头的填充词）。kinds 决定按哪类指令头解析。"""
        if not isinstance(text, str) or not text.strip():
            return ""
        if len(kinds) > 1:
            pattern = ALL_QUERY_PATTERN
        elif kinds and kinds[0] == GROK2API_SEARCH_X:
            pattern = X_QUERY_PATTERN
        else:
            pattern = WEB_QUERY_PATTERN
        m = pattern.match(text.strip())
        query = (m.group(1) or "").strip() if m else ""
        return QUERY_FILLER_PATTERN.sub("", query).strip()

    @staticmethod
    def _quoted_context(event: AstrMessageEvent) -> str:
        """若指令是回复某条消息发出的，提取被回复文本作为搜索上下文（尽力而为）。"""
        try:
            chain = event.get_messages()
        except Exception:
            chain = getattr(event.message_obj, "message", []) if hasattr(event, "message_obj") else []
        for seg in chain or []:
            try:
                if not isinstance(seg, Comp.Reply):
                    continue
                parts: List[str] = []
                for s in getattr(seg, "chain", None) or []:
                    txt = getattr(s, "text", None)
                    if isinstance(txt, str) and txt.strip():
                        parts.append(txt.strip())
                if parts:
                    return "\n".join(parts)
                ms = getattr(seg, "message_str", None)
                if isinstance(ms, str) and ms.strip():
                    return ms.strip()
            except Exception:
                continue
        return ""

    # === 指令入口 ===

    @filter.command("search", alias={"搜索", "联网搜索"})
    async def web_search(self, event: AstrMessageEvent):
        async for r in self._handle_search(event, [GROK2API_SEARCH_WEB]):
            yield r

    @filter.command("xsearch", alias={"x搜索", "推特搜索", "搜推特"})
    async def x_search(self, event: AstrMessageEvent):
        async for r in self._handle_search(event, [GROK2API_SEARCH_X]):
            yield r

    @filter.command("gsearch", alias={"allsearch", "全搜", "全网搜索", "混合搜索"})
    async def global_search(self, event: AstrMessageEvent):
        """组合搜索：一次请求同时开启联网搜索与 X 搜索，由模型综合两边信息回答。"""
        async for r in self._handle_search(event, [GROK2API_SEARCH_WEB, GROK2API_SEARCH_X]):
            yield r

    async def _handle_search(self, event: AstrMessageEvent, kinds: List[str]):
        text = self._message_text(event)
        query = self.extract_query(text, kinds)
        if not query:
            yield event.plain_result(USAGE_HINT)
            try:
                event.stop_event()
            except Exception:
                pass
            return

        client = self._build_client()
        if client is None:
            yield event.plain_result(
                "尚未配置 grok2api：请在插件配置中填写 grok2api_base_url 与 grok2api_api_key。"
            )
            try:
                event.stop_event()
            except Exception:
                pass
            return

        context_text = self._quoted_context(event)
        full_query = query
        if context_text:
            full_query += f"\n\n【上下文信息】\n{context_text}"

        start_ts = time.perf_counter()
        try:
            reply_text, citations = await client.search(
                user_prompt=build_search_user_prompt(
                    full_query,
                    search_x=(GROK2API_SEARCH_X in kinds),
                    search_all=(len(kinds) > 1),
                ),
                system_prompt=build_search_system_prompt(),
                kinds=kinds,
                x_from_date=self._get_conf_str(GROK2API_X_FROM_DATE_KEY),
                x_to_date=self._get_conf_str(GROK2API_X_TO_DATE_KEY),
            )
        except GrokSearchError as e:
            logger.error("grok_search: search failed: %s", e)
            hint = ""
            if GROK2API_SEARCH_X in kinds:
                hint = "\n提示：X 搜索需要 grok2api 内的账号类型支持（Web 类账号仅支持联网搜索）。"
            yield event.plain_result(f"搜索失败：{str(e)[:300]}{hint}")
            try:
                event.stop_event()
            except Exception:
                pass
            return
        except Exception as e:
            logger.error("grok_search: unexpected error: %s", e)
            yield event.plain_result("搜索失败：发生内部异常，请稍后再试或检查插件日志。")
            try:
                event.stop_event()
            except Exception:
                pass
            return

        out = demote_markdown_to_text(normalize_link_spacing(reply_text))
        cites = format_citations(citations)
        if cites:
            out = f"{out}\n\n{cites}"
        if self._get_conf_bool(SHOW_COST_KEY, True):
            elapsed = time.perf_counter() - start_ts
            if isinstance(elapsed, (int, float)) and elapsed > 0:
                out = f"{out}\n\ncost: {elapsed:.3f}s"

        yield event.plain_result(out)
        try:
            event.stop_event()
        except Exception:
            pass

    async def terminate(self):
        return
