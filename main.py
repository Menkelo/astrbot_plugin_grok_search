from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional, Any, Dict, Set, Union
import os
import asyncio
import re
import shutil
import time
import base64

from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star
from astrbot.api import logger
import astrbot.api.message_components as Comp

from .message_utils import (
    extract_quoted_payload,
    extract_text_and_images_from_chain,
    call_get_msg,
    ob_data,
    napcat_resolve_file_url,
    extract_from_onebot_message_payload,
)
from .prompt_utils import (
    build_user_prompt,
    build_system_prompt,
    build_search_user_prompt,
    build_search_system_prompt,
)
from .grok_client import (
    GrokChatClient,
    GrokSearchError,
    GROK2API_SEARCH_WEB,
    GROK2API_SEARCH_X,
    GROK2API_SEARCH_ALL,
    format_citations,
)
from .formatter import normalize_link_spacing, demote_markdown_to_text
from .file_preview_utils import (
    build_text_exts_from_config,
    extract_file_preview_from_reply,
)

KEYWORD_ZSSM_ENABLE_KEY = "enable_keyword_zssm"
FILE_PREVIEW_EXTS_KEY = "file_preview_exts"
FILE_PREVIEW_MAX_SIZE_KB_KEY = "file_preview_max_size_kb"

# grok2api 直连配置
GROK2API_BASE_URL_KEY = "grok2api_base_url"
GROK2API_API_KEY_KEY = "grok2api_api_key"
GROK2API_MODEL_KEY = "grok2api_model"
LLM_TIMEOUT_SEC_KEY = "llm_timeout_sec"
LLM_RETRY_TIMES_KEY = "llm_retry_times"
SHOW_COST_KEY = "show_cost"

DEFAULT_GROK2API_MODEL = "grok-4"
DEFAULT_LLM_TIMEOUT_SEC = 90
DEFAULT_LLM_RETRY_TIMES = 2

DEFAULT_FILE_PREVIEW_EXTS = "txt,md,log,json,csv,ini,cfg,yml,yaml,py"
DEFAULT_FILE_PREVIEW_MAX_SIZE_KB = 100

THINKING_GIF_PATH = os.path.join(os.path.dirname(__file__), "thinking.gif")

ZSSM_HANDLED_KEY = "zssm_handled"

ZSSM_TRIGGER_PATTERN = re.compile(r"^[\s/!！。\.、，\-]{0,10}zssm(?:[\s:：?？,，.。;；\-!！]+|$)", re.I)
ZSSM_COMMAND_PATTERN = re.compile(r"^\s*/\s*zssm(?:\s|$)", re.I)
ZSSM_CONTENT_PATTERN = re.compile(r"^[\s/!！。\.、，\-]{0,10}zssm(?:[\s:：?？,，.。;；\-!！]+\s*)?([\s\S]*)$", re.I)
BRACKET_IMAGE_PATTERN = re.compile(r"[\[【](图片|image|img|文件|file)[\]】]", flags=re.I)
MULTI_SPACE_PATTERN = re.compile(r"\s{2,}")
EXPLICIT_SEARCH_PATTERN = re.compile(
    r"^(?:联网搜索|帮我搜索|帮我查|查一下|搜索|联网|search)\s*[:：]?\s*(?:一下|看看)?\s*([\s\S]+)$",
    re.I,
)
# X（推特）搜索指令：如“x搜索 xxx”“搜一下推特 xxx”“推特搜索 xxx”。
# 第二种语序要求 x/推特 后紧跟非字母数字（避免误匹配“查xlsx怎么用”这类词）。
EXPLICIT_X_SEARCH_PATTERN = re.compile(
    r"^(?:(?:x|twi(?:tter)?|推特)\s*(?:搜索|搜|查找|查)"
    r"|(?:搜索|搜|查找|查)\s*(?:一下)?\s*(?:x|twi(?:tter)?|推特)(?=[^a-z0-9]|$))"
    r"\s*(?:一下|看看|关于|相关|上|的)*\s*[:：]?\s*([\s\S]+)$",
    re.I,
)
# 组合搜索指令：一次请求同时开启联网搜索 + X 搜索
EXPLICIT_ALL_SEARCH_PATTERN = re.compile(
    r"^(?:全搜|全网搜索|全网搜|混合搜索|综合搜索)\s*[:：]?\s*(?:一下)?\s*([\s\S]+)$",
    re.I,
)


@dataclass
class LLMPlan:
    user_prompt: str
    images: List[str] = field(default_factory=list)
    cleanup_paths: List[str] = field(default_factory=list)
    is_search: bool = False  # True=识别到在线搜索指令，走 grok2api 搜索工具
    search_kind: str = ""  # ""=未识别；web=联网搜索；x=X搜索；all=组合搜索
    search_query: str = ""  # 从指令中提取的搜索词（去除“搜索/x搜索”等前缀）
    search_context: str = ""  # 搜索时携带的被回复消息上下文
    concise_mode: bool = True  # True=100字逻辑；False=详细不限字数


@dataclass
class ReplyPlan:
    message: str
    stop_event: bool = True
    cleanup_paths: List[str] = field(default_factory=list)


ExplainPlan = Union[LLMPlan, ReplyPlan]


class ZssmGrokPlugin(Star):
    """zssm 的 grok2api 版：解释文本/图片/文件/合并转发，支持联网搜索、X 搜索与组合搜索。"""

    def __init__(self, context: Context, config: Optional[Dict[str, Any]] = None):
        super().__init__(context)
        self.config: Dict[str, Any] = config if config is not None else {}

    # === 基础工具 ===

    def _reply_text_result(self, event: AstrMessageEvent, text: str):
        safe_text = str(text).strip() if text is not None else ""
        return event.plain_result(safe_text)

    def _get_conf_str(self, key: str, default: str) -> str:
        try:
            v = self.config.get(key)
            if isinstance(v, str):
                return v.strip()
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

    # === 触发与内容解析 ===

    @staticmethod
    def _is_zssm_trigger(text: str) -> bool:
        if not isinstance(text, str):
            return False
        t = text.strip()
        if len(t) > 500:
            t = t[:500]
        return bool(ZSSM_TRIGGER_PATTERN.match(t))

    @staticmethod
    def _first_plain_head_text(chain: List[object]) -> str:
        if not isinstance(chain, list):
            return ""
        for seg in chain:
            try:
                if isinstance(seg, Comp.Plain):
                    txt = getattr(seg, "text", None)
                    if isinstance(txt, str) and txt.strip():
                        return txt
            except Exception:
                continue
        return ""

    @staticmethod
    def _chain_has_at_me(chain: List[object], self_id: str) -> bool:
        if not isinstance(chain, list):
            return False
        for seg in chain:
            try:
                if isinstance(seg, Comp.At):
                    qq = getattr(seg, "qq", None)
                    if qq is not None and str(qq) == str(self_id):
                        return True
            except Exception:
                continue
        return False

    def _already_handled(self, event: AstrMessageEvent, key: str = ZSSM_HANDLED_KEY) -> bool:
        try:
            extras = event.get_extra()
            if isinstance(extras, dict) and extras.get(key):
                return True
        except Exception:
            pass
        try:
            event.set_extra(key, True)
        except Exception:
            pass
        return False

    @staticmethod
    def _strip_trigger_and_get_content(text: str) -> str:
        if not isinstance(text, str):
            return ""
        t = text.strip()
        if len(t) > 2000:
            t = t[:2000]
        m = ZSSM_CONTENT_PATTERN.match(t)
        if not m:
            return ""
        content = (m.group(1) or "").strip()
        content = BRACKET_IMAGE_PATTERN.sub(" ", content)
        content = MULTI_SPACE_PATTERN.sub(" ", content).strip()
        return content

    def _get_inline_content(self, event: AstrMessageEvent) -> str:
        chain = self._safe_get_chain(event)
        head = self._first_plain_head_text(chain)
        if head:
            c = self._strip_trigger_and_get_content(head)
            if c:
                return c
        try:
            s = event.get_message_str()
        except Exception:
            s = getattr(event, "message_str", "") or ""
        return self._strip_trigger_and_get_content(s)

    @staticmethod
    def _safe_get_chain(event: AstrMessageEvent) -> List[object]:
        try:
            return event.get_messages()
        except Exception:
            return getattr(event.message_obj, "message", []) if hasattr(event, "message_obj") else []

    def _extract_images_from_event(self, event: AstrMessageEvent) -> List[str]:
        chain = self._safe_get_chain(event)
        try:
            _t, images = extract_text_and_images_from_chain(chain)
        except Exception:
            images = []
        return [x for x in images if isinstance(x, str) and x]

    # === 搜索指令识别 ===

    @staticmethod
    def _decide_search_kind(inline: str) -> str:
        """识别显式在线搜索指令，返回搜索类型：""=非搜索；web=联网；x=X；all=组合。"""
        if not inline:
            return ""
        if EXPLICIT_ALL_SEARCH_PATTERN.match(inline):
            return GROK2API_SEARCH_ALL
        if EXPLICIT_X_SEARCH_PATTERN.match(inline):
            return GROK2API_SEARCH_X
        if EXPLICIT_SEARCH_PATTERN.match(inline):
            return GROK2API_SEARCH_WEB
        return ""

    @classmethod
    def _decide_search(cls, inline: str) -> bool:
        """识别显式在线搜索指令（如“搜索/查一下/联网/x搜索/全搜”）。"""
        return cls._decide_search_kind(inline) != ""

    @staticmethod
    def _extract_search_query(inline: str) -> str:
        """从搜索指令中提取实际搜索词，如“搜索一下今天的天气”->“今天的天气”。"""
        if not inline:
            return ""
        m = (
            EXPLICIT_ALL_SEARCH_PATTERN.match(inline)
            or EXPLICIT_X_SEARCH_PATTERN.match(inline)
            or EXPLICIT_SEARCH_PATTERN.match(inline)
        )
        if m:
            q = (m.group(1) or "").strip()
            if q:
                return q
        return inline

    # === 图片解析 ===

    async def _resolve_images_for_llm(self, event: AstrMessageEvent, images: List[str]) -> List[str]:
        def _norm(x: object) -> Optional[str]:
            if not isinstance(x, str) or not x:
                return None
            s = x.strip()
            if not s:
                return None
            ls = s.lower()
            if ls.startswith(("http://", "https://", "base64://", "data:image/")):
                return s
            if ls.startswith("file://"):
                fp = s[7:]
                if fp.startswith("/") and len(fp) > 3 and fp[2] == ":":
                    fp = fp[1:]
                if fp and os.path.exists(fp):
                    return os.path.abspath(fp)
                return None
            if os.path.exists(s):
                return os.path.abspath(s)
            return None

        resolved: List[str] = []
        seen: Set[str] = set()

        def _add(c: str):
            if c and c not in seen:
                seen.add(c)
                resolved.append(c)

        resolve_candidates: List[str] = []
        for img in images:
            if not isinstance(img, str) or not img:
                continue
            d = _norm(img)
            if d:
                _add(d)
            else:
                resolve_candidates.append(img)

        if resolve_candidates:
            sem = asyncio.Semaphore(6)

            async def _resolve_one(fid: str) -> Optional[str]:
                async with sem:
                    try:
                        return await napcat_resolve_file_url(event, fid)
                    except Exception as e:
                        logger.debug(f"zssm_grok: resolve file url failed: {e}")
                        return None

            rs = await asyncio.gather(
                *[_resolve_one(fid) for fid in resolve_candidates],
                return_exceptions=True
            )
            for r in rs:
                if isinstance(r, str) and r:
                    nr = _norm(r)
                    if nr:
                        _add(nr)

        # fallback: 尝试从当前消息 get_msg 再捞一次图片
        if hasattr(event, "message_obj"):
            try:
                mid = str(getattr(event.message_obj, "message_id", "") or "")
                if mid:
                    ret = await call_get_msg(event, mid)
                    data = ob_data(ret or {})
                    _t, imgs2 = extract_from_onebot_message_payload(data)
                    for x in imgs2:
                        nx = _norm(x)
                        if nx:
                            _add(nx)
            except Exception:
                pass

        return resolved

    # === 群文件预览配置 ===

    def _get_file_preview_exts(self) -> Set[str]:
        raw = self._get_conf_str(FILE_PREVIEW_EXTS_KEY, DEFAULT_FILE_PREVIEW_EXTS)
        base_default = [ext.strip() for ext in DEFAULT_FILE_PREVIEW_EXTS.split(",") if ext.strip()]
        return build_text_exts_from_config(raw, base_default)

    def _get_file_preview_max_bytes(self) -> Optional[int]:
        try:
            kb = self._get_conf_int(
                FILE_PREVIEW_MAX_SIZE_KB_KEY,
                DEFAULT_FILE_PREVIEW_MAX_SIZE_KB,
                1,
                1024 * 1024,
            )
            return int(kb) * 1024
        except Exception:
            return None

    def _build_system_prompt(self) -> str:
        return build_system_prompt()

    # === 处理中提示图 ===

    def _load_thinking_gif_base64(self) -> Optional[str]:
        try:
            abs_path = os.path.abspath(THINKING_GIF_PATH)
            if not os.path.isfile(abs_path):
                return None
            with open(abs_path, "rb") as f:
                raw = f.read()
            if not raw:
                return None
            return "base64://" + base64.b64encode(raw).decode("ascii")
        except Exception as e:
            logger.debug(f"zssm_grok: load thinking.gif failed: {e}")
            return None

    async def _send_processing_image_notice(self, event: AstrMessageEvent) -> None:
        try:
            if not (
                hasattr(event, "bot")
                and hasattr(event.bot, "api")
                and hasattr(event.bot.api, "call_action")
            ):
                return
            b64_file = self._load_thinking_gif_base64()
            if not b64_file:
                return

            message = [{"type": "image", "data": {"file": b64_file}}]

            gid = None
            try:
                gid = event.get_group_id()
            except Exception:
                pass

            if gid is not None and str(gid) != "":
                group_id = int(gid) if str(gid).isdigit() else gid
                await event.bot.api.call_action(
                    "send_msg",
                    message_type="group",
                    group_id=group_id,
                    message=message,
                )
                return

            uid = None
            try:
                uid = event.get_sender_id()
            except Exception:
                pass

            if uid is not None and str(uid) != "":
                user_id = int(uid) if str(uid).isdigit() else uid
                await event.bot.api.call_action(
                    "send_msg",
                    message_type="private",
                    user_id=user_id,
                    message=message,
                )
        except Exception as e:
            logger.debug(f"zssm_grok: send processing image failed: {e}")

    # === 计划构建 ===

    async def _build_explain_plan(self, event: AstrMessageEvent, *, inline: str) -> ExplainPlan:
        cleanup_paths: List[str] = []

        q_text, q_images, _from_forward = await extract_quoted_payload(event)
        current_images_raw = self._extract_images_from_event(event)

        # 把“被回复消息里的图片(q_images)”也一起做 URL 解析
        all_images_raw = (q_images or []) + current_images_raw
        try:
            all_images = await self._resolve_images_for_llm(event, all_images_raw)
        except Exception:
            all_images = []
        all_images = list(dict.fromkeys(all_images))

        try:
            file_preview = await extract_file_preview_from_reply(
                event,
                text_exts=self._get_file_preview_exts(),
                max_size_bytes=self._get_file_preview_max_bytes(),
            )
            if file_preview:
                q_text = f"{file_preview}\n\n{q_text}" if q_text else file_preview
        except Exception as e:
            logger.debug(f"zssm_grok: file preview extraction failed: {e}")

        logger.info(
            "zssm_grok: quoted text len=%s, quoted images=%s, inline=%s",
            len(q_text or ""),
            len(q_images or []),
            bool(inline),
        )

        # 有 inline（zssm 问题 / zssm 问题+引用）=> 详细不限字数
        if inline:
            prompt = inline
            search_kind = self._decide_search_kind(inline)
            if q_text:
                prompt += f"\n\n【上下文信息】\n{q_text}"
            return LLMPlan(
                user_prompt=prompt,
                images=all_images,
                cleanup_paths=cleanup_paths,
                is_search=search_kind != "",
                search_kind=search_kind,
                search_query=self._extract_search_query(inline) if search_kind else "",
                search_context=q_text or "",
                concise_mode=False,
            )

        # 仅 zssm + 引用 => 100字逻辑
        if q_text or all_images:
            user_prompt = build_user_prompt(q_text, all_images, concise=True)
            return LLMPlan(
                user_prompt=user_prompt,
                images=all_images,
                cleanup_paths=cleanup_paths,
                is_search=False,
                concise_mode=True,
            )

        return ReplyPlan(
            message="请输入要解释的内容，或回复一条消息/图片/文件进行解释。",
            stop_event=True,
            cleanup_paths=cleanup_paths,
        )

    # === 执行 ===

    def _build_client(self) -> Optional[GrokChatClient]:
        base_url = self._get_conf_str(GROK2API_BASE_URL_KEY, "")
        api_key = self._get_conf_str(GROK2API_API_KEY_KEY, "")
        if not base_url or not api_key:
            return None
        model = self._get_conf_str(GROK2API_MODEL_KEY, "") or DEFAULT_GROK2API_MODEL
        return GrokChatClient(
            base_url=base_url,
            api_key=api_key,
            model=model,
            timeout_sec=self._get_conf_int(LLM_TIMEOUT_SEC_KEY, DEFAULT_LLM_TIMEOUT_SEC, 5, 600),
            retry_times=self._get_conf_int(LLM_RETRY_TIMES_KEY, DEFAULT_LLM_RETRY_TIMES, 1, 5),
            logger=logger,
        )

    async def _execute_explain_plan(self, event: AstrMessageEvent, plan: ExplainPlan):
        if isinstance(plan, ReplyPlan):
            yield self._reply_text_result(event, plan.message)
            if plan.stop_event:
                try:
                    event.stop_event()
                except Exception:
                    pass
            return

        client = self._build_client()
        if client is None:
            yield self._reply_text_result(
                event, "尚未配置 grok2api：请在插件配置中填写 grok2api_base_url 与 grok2api_api_key。"
            )
            try:
                event.stop_event()
            except Exception:
                pass
            return

        try:
            await self._send_processing_image_notice(event)
            start_ts = time.perf_counter()

            if plan.is_search:
                query = plan.search_query or plan.user_prompt
                if plan.search_context:
                    query += f"\n\n【上下文信息】\n{plan.search_context}"
                reply_text, citations = await client.chat(
                    user_prompt=build_search_user_prompt(query, plan.search_kind),
                    system_prompt=build_search_system_prompt(),
                    kinds=[plan.search_kind],
                )
            else:
                reply_text, citations = await client.chat(
                    user_prompt=plan.user_prompt,
                    system_prompt=self._build_system_prompt(),
                    image_specs=plan.images,
                )

            out = demote_markdown_to_text(normalize_link_spacing(reply_text))
            cites = format_citations(citations)
            if cites:
                out = f"{out}\n\n{cites}"
            if self._get_conf_bool(SHOW_COST_KEY, True):
                elapsed = time.perf_counter() - start_ts
                if isinstance(elapsed, (int, float)) and elapsed > 0:
                    out = f"{out}\n\ncost: {elapsed:.3f}s"

            yield self._reply_text_result(event, out)

            try:
                event.stop_event()
            except Exception:
                pass

        except GrokSearchError as e:
            logger.error("zssm_grok: grok2api request failed: %s", e)
            hint = ""
            if plan.is_search and plan.search_kind in (GROK2API_SEARCH_X, GROK2API_SEARCH_ALL):
                hint = "\n提示：X 搜索需要 grok2api 内的账号类型支持（Web 类账号仅支持联网搜索）。"
            yield self._reply_text_result(event, f"请求失败：{str(e)[:300]}{hint}")
            try:
                event.stop_event()
            except Exception:
                pass
        except asyncio.TimeoutError:
            yield self._reply_text_result(event, "请求超时，请稍后重试。")
            try:
                event.stop_event()
            except Exception:
                pass
        except Exception as e:
            logger.error(f"zssm_grok: LLM 调用失败: {e}")
            yield self._reply_text_result(event, "处理失败：模型调用异常，请稍后再试或联系管理员。")
            try:
                event.stop_event()
            except Exception:
                pass

    def _cleanup_paths(self, paths: List[str]) -> None:
        for p in paths:
            if not isinstance(p, str) or not p:
                continue
            try:
                if os.path.isdir(p):
                    shutil.rmtree(p, ignore_errors=True)
                elif os.path.isfile(p):
                    os.remove(p)
            except OSError as e:
                logger.debug(f"zssm_grok: cleanup path failed: {p}, error: {e}")

    # === 指令入口 ===

    @filter.command("zssm", alias={"知识说明", "解释"})
    async def zssm(self, event: AstrMessageEvent):
        cleanup_paths: List[str] = []
        try:
            if self._already_handled(event, ZSSM_HANDLED_KEY):
                return

            inline = self._get_inline_content(event)
            plan = await self._build_explain_plan(event, inline=inline)
            cleanup_paths = list(getattr(plan, "cleanup_paths", []) or [])

            async for r in self._execute_explain_plan(event, plan):
                yield r

        except Exception as e:
            logger.error("zssm_grok: handler crashed: %s", e)
            yield self._reply_text_result(event, "解释失败：插件内部异常，请稍后再试或联系管理员。")
            try:
                event.stop_event()
            except Exception:
                pass
        finally:
            self._cleanup_paths(cleanup_paths)

    async def terminate(self):
        return

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def keyword_trigger(self, event: AstrMessageEvent):
        chain = self._safe_get_chain(event)
        head = self._first_plain_head_text(chain)

        at_me = False
        try:
            self_id = event.get_self_id()
            at_me = self._chain_has_at_me(chain, self_id)
        except Exception:
            pass

        if isinstance(head, str) and head.strip():
            text = head.strip()
        else:
            try:
                text = event.get_message_str()
            except Exception:
                text = getattr(event, "message_str", "") or ""
            text = text.strip() if isinstance(text, str) else ""

        if not text:
            return
        if ZSSM_COMMAND_PATTERN.match(text):
            return
        if at_me and re.match(r"^zssm(?:\s|$)", text, re.I):
            return

        if self._get_conf_bool(KEYWORD_ZSSM_ENABLE_KEY, True) and self._is_zssm_trigger(text):
            async for r in self.zssm(event):
                yield r
            return
