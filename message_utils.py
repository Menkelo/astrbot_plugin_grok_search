from __future__ import annotations

from typing import List, Tuple, Optional, Any, Dict
import html
import json
import os

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent
import astrbot.api.message_components as Comp

MAX_FORWARD_RECURSION_DEPTH = 15

# 事件内缓存（避免同一次处理重复 get_msg/get_forward_msg）
EVENT_CACHE_KEY = "__zssm_event_cache__"


def _ensure_event_cache(event: AstrMessageEvent) -> Dict[str, Any]:
    cache: Dict[str, Any] = {}
    try:
        extras = event.get_extra()
        if isinstance(extras, dict):
            c = extras.get(EVENT_CACHE_KEY)
            if isinstance(c, dict):
                return c
    except Exception:
        pass

    try:
        event.set_extra(EVENT_CACHE_KEY, cache)
    except Exception:
        pass
    return cache


def _cache_get(event: AstrMessageEvent, key: str) -> Any:
    cache = _ensure_event_cache(event)
    return cache.get(key)


def _cache_set(event: AstrMessageEvent, key: str, value: Any) -> None:
    cache = _ensure_event_cache(event)
    cache[key] = value
    try:
        event.set_extra(EVENT_CACHE_KEY, cache)
    except Exception:
        pass


def _extract_text_from_segment(seg: object, texts: List[str], images: List[str]) -> None:
    try:
        if isinstance(seg, Comp.Plain):
            txt = getattr(seg, "text", None)
            if isinstance(txt, str) and txt:
                texts.append(txt)
        elif isinstance(seg, Comp.Image):
            _extract_image_urls(seg, images)
    except (AttributeError, TypeError) as e:
        logger.debug(f"zssm_core: extract segment failed: {e}")


def _extract_image_urls(seg: object, images: List[str]) -> None:
    candidates: List[str] = []
    for key in ("url", "file", "path", "src", "base64", "data"):
        v = getattr(seg, key, None)
        if isinstance(v, str) and v:
            candidates.append(v)

    d = getattr(seg, "data", None)
    if isinstance(d, dict):
        for key in ("url", "file", "path", "src", "base64", "data"):
            v = d.get(key)
            if isinstance(v, str) and v:
                candidates.append(v)

    seen_local: set = set()
    for c in candidates:
        if c not in seen_local:
            seen_local.add(c)
            images.append(c)


def _extract_nested_content(seg: object, texts: List[str], images: List[str]) -> None:
    content = None

    if hasattr(Comp, "Node") and isinstance(seg, getattr(Comp, "Node")):
        content = getattr(seg, "content", None)
    elif hasattr(Comp, "Nodes") and isinstance(seg, getattr(Comp, "Nodes")):
        nodes = getattr(seg, "nodes", None) or getattr(seg, "content", None)
        if isinstance(nodes, list):
            for node in nodes:
                c = getattr(node, "content", None)
                if isinstance(c, list):
                    t2, i2 = extract_text_and_images_from_chain(c)
                    if t2:
                        texts.append(t2)
                    images.extend(i2)
        return
    elif hasattr(Comp, "Forward") and isinstance(seg, getattr(Comp, "Forward")):
        nodes = getattr(seg, "nodes", None) or getattr(seg, "content", None)
        if isinstance(nodes, list):
            for node in nodes:
                c = getattr(node, "content", None)
                if isinstance(c, list):
                    t2, i2 = extract_text_and_images_from_chain(c)
                    if t2:
                        texts.append(t2)
                    images.extend(i2)
        return

    if isinstance(content, list):
        t2, i2 = extract_text_and_images_from_chain(content)
        if t2:
            texts.append(t2)
        images.extend(i2)


def extract_text_and_images_from_chain(chain: List[object]) -> Tuple[str, List[str]]:
    texts: List[str] = []
    images: List[str] = []
    if not isinstance(chain, list):
        return ("", images)

    for seg in chain:
        try:
            _extract_text_from_segment(seg, texts, images)
            _extract_nested_content(seg, texts, images)
        except (AttributeError, TypeError, ValueError) as e:
            logger.debug(f"zssm_core: parse chain segment failed: {e}")

    return ("\n".join([t for t in texts if t]).strip(), images)


def chain_has_forward(chain: List[object]) -> bool:
    if not isinstance(chain, list):
        return False
    for seg in chain:
        try:
            if hasattr(Comp, "Forward") and isinstance(seg, getattr(Comp, "Forward")):
                return True
            if hasattr(Comp, "Node") and isinstance(seg, getattr(Comp, "Node")):
                return True
            if hasattr(Comp, "Nodes") and isinstance(seg, getattr(Comp, "Nodes")):
                return True
        except (AttributeError, TypeError):
            continue
    return False


def _strip_bracket_prefix(text: str) -> str:
    if not isinstance(text, str):
        return ""
    s = text.strip()
    if not s:
        return ""
    if s.startswith("["):
        end = s.find("]")
        if end != -1:
            return s[end + 1:].strip()
    if s.startswith("【"):
        end = s.find("】")
        if end != -1:
            return s[end + 1:].strip()
    return s


def _format_json_share(data: Dict[str, Any]) -> str:
    if not isinstance(data, dict):
        return ""

    app = data.get("app") or ""

    if app == "com.tencent.multimsg":
        prompt = data.get("prompt") or data.get("desc") or "[聊天记录]"
        detail = data.get("meta", {}).get("detail", {}) or {}
        summary = detail.get("summary") or ""
        source = detail.get("source") or ""
        lines = [str(prompt).strip() or "[聊天记录]"]
        if source:
            lines.append(f"来源: {source}")
        if summary:
            lines.append(f"摘要: {summary}")
        return "\n".join(lines)

    if app == "com.tencent.miniapp_01":
        detail = data.get("meta", {}).get("detail_1", {}) or {}
        raw_prompt = str(data.get("prompt") or "").strip()
        title = _strip_bracket_prefix(raw_prompt) or detail.get("desc") or "无标题"
        desc = detail.get("desc") or ""
        url = detail.get("qqdocurl") or detail.get("url") or ""
        preview = detail.get("preview") or ""
        app_title = detail.get("title") or "小程序"

        lines = [f"[小程序分享 - {app_title}]", f"标题: {title}"]
        if desc:
            lines.append(f"简介: {desc}")
        if url:
            lines.append(f"跳转链接: {url}")
        if preview:
            lines.append(f"封面图: {preview}")
        return "\n".join(lines)

    if app == "com.tencent.tuwen.lua":
        news = data.get("meta", {}).get("news", {}) or {}
        title = news.get("title") or "无标题"
        desc = news.get("desc") or ""
        url = news.get("jumpUrl") or ""
        preview = news.get("preview") or ""
        tag = news.get("tag") or "图文消息"
        lines = [f"[图文/小程序分享 - {tag}]", f"标题: {title}"]
        if desc:
            lines.append(f"简介: {desc}")
        if url:
            lines.append(f"跳转链接: {url}")
        if preview:
            lines.append(f"封面图: {preview}")
        return "\n".join(lines)

    prompt = data.get("prompt") or data.get("desc") or ""
    return str(prompt) if prompt else ""


def try_extract_from_reply_component(reply_comp: object) -> Tuple[Optional[str], List[str], bool]:
    for attr in ("chain", "message", "origin", "content"):
        payload = getattr(reply_comp, attr, None)
        if isinstance(payload, list):
            text, images = extract_text_and_images_from_chain(payload)
            has_forward = chain_has_forward(payload)
            return (text, images, has_forward)
    return (None, [], False)


def get_reply_message_id(reply_comp: object) -> Optional[str]:
    for key in ("id", "message_id", "reply_id", "messageId", "message_seq"):
        val = getattr(reply_comp, key, None)
        if isinstance(val, (str, int)) and str(val):
            return str(val)
    data = getattr(reply_comp, "data", None)
    if isinstance(data, dict):
        for key in ("id", "message_id", "reply", "messageId", "message_seq"):
            val = data.get(key)
            if isinstance(val, (str, int)) and str(val):
                return str(val)
    return None


def ob_data(obj: Any) -> Dict[str, Any]:
    if isinstance(obj, dict):
        data = obj.get("data")
        if isinstance(data, dict):
            return data
        return obj
    return {}


async def call_get_msg(event: AstrMessageEvent, message_id: str) -> Optional[Dict[str, Any]]:
    if not (isinstance(message_id, str) and message_id.strip()):
        return None
    if (
        not hasattr(event, "bot")
        or not hasattr(event.bot, "api")
        or not hasattr(event.bot.api, "call_action")
    ):
        return None

    mid = message_id.strip()
    params_list: List[Dict[str, Any]] = [
        {"message_id": mid},
        {"id": mid},
    ]
    if mid.isdigit():
        params_list.insert(1, {"message_id": int(mid)})
        params_list.append({"id": int(mid)})

    last_err: Optional[Exception] = None
    for params in params_list:
        try:
            return await event.bot.api.call_action("get_msg", **params)
        except Exception as e:
            last_err = e
            continue

    if last_err:
        logger.warning(f"zssm_core: get_msg failed for {mid}: {last_err}")
    return None


async def call_get_forward_msg(event: AstrMessageEvent, forward_id: str) -> Optional[Dict[str, Any]]:
    if not (isinstance(forward_id, str) and forward_id.strip()):
        return None
    if not hasattr(event, "bot") or not hasattr(event.bot, "api") or not hasattr(event.bot.api, "call_action"):
        return None

    fid = forward_id.strip()
    params_list: List[Dict[str, Any]] = [
        {"message_id": fid},
        {"id": fid},
    ]
    if fid.isdigit():
        params_list.insert(1, {"message_id": int(fid)})
        params_list.append({"id": int(fid)})

    last_err: Optional[Exception] = None
    for params in params_list:
        try:
            return await event.bot.api.call_action("get_forward_msg", **params)
        except Exception as e:
            last_err = e
            continue

    if last_err:
        logger.warning(f"zssm_core: get_forward_msg failed for {fid}: {last_err}")
    return None


async def cached_call_get_msg(
    event: AstrMessageEvent, message_id: str
) -> Optional[Dict[str, Any]]:
    mid = str(message_id).strip()
    if not mid:
        return None
    ck = f"get_msg:{mid}"
    cached = _cache_get(event, ck)
    if isinstance(cached, dict):
        return cached

    ret = await call_get_msg(event, mid)
    if isinstance(ret, dict):
        _cache_set(event, ck, ret)
    return ret


async def cached_call_get_forward_msg(
    event: AstrMessageEvent, forward_id: str
) -> Optional[Dict[str, Any]]:
    fid = str(forward_id).strip()
    if not fid:
        return None
    ck = f"get_forward_msg:{fid}"
    cached = _cache_get(event, ck)
    if isinstance(cached, dict):
        return cached

    ret = await call_get_forward_msg(event, fid)
    if isinstance(ret, dict):
        _cache_set(event, ck, ret)
    return ret


def extract_from_onebot_message_payload(payload: Any) -> Tuple[str, List[str]]:
    texts: List[str] = []
    images: List[str] = []
    data = ob_data(payload) if isinstance(payload, dict) else {}
    if isinstance(data, dict):
        msg = data.get("message") or data.get("messages")
        if isinstance(msg, list):
            for seg in msg:
                try:
                    if not isinstance(seg, dict):
                        continue
                    t = seg.get("type")
                    d = seg.get("data", {}) if isinstance(seg.get("data"), dict) else {}
                    if t in ("text", "plain"):
                        txt = d.get("text")
                        if isinstance(txt, str) and txt:
                            texts.append(txt)
                    elif t == "image":
                        url = d.get("url") or d.get("file")
                        if isinstance(url, str) and url:
                            images.append(url)
                    elif t == "json":
                        raw = d.get("data")
                        if isinstance(raw, str) and raw.strip():
                            try:
                                inner = json.loads(raw)
                                summary = _format_json_share(inner)
                                if summary:
                                    texts.append(summary)
                            except json.JSONDecodeError as e:
                                logger.debug(f"zssm_core: parse json segment failed: {e}")
                    elif t == "file":
                        name = d.get("name") or d.get("file") or "未命名文件"
                        summary = d.get("summary") or ""
                        file_id = d.get("file") or ""
                        parts = [f"[群文件] {name}"]
                        if summary:
                            parts.append(f"说明: {summary}")
                        if file_id:
                            parts.append(f"文件标识: {file_id}")
                        texts.append("\n".join(parts))
                except Exception as e:
                    logger.debug(f"zssm_core: parse onebot segment failed: {e}")
            return ("\n".join([t for t in texts if t]).strip(), images)
        elif isinstance(msg, str) and msg:
            texts.append(msg)
            return ("\n".join(texts).strip(), images)
        raw = data.get("raw_message")
        if isinstance(raw, str) and raw:
            texts.append(raw)
            return ("\n".join(texts).strip(), images)
    return ("", images)


def _extract_forward_nodes_recursively(
    message_nodes: List[Any],
    texts: List[str],
    images: List[str],
    depth: int = 0,
) -> None:
    if not isinstance(message_nodes, list):
        return

    if depth > MAX_FORWARD_RECURSION_DEPTH:
        logger.warning(f"zssm_core: forward recursion depth exceeded {MAX_FORWARD_RECURSION_DEPTH}")
        return

    indent = "  " * depth

    for message_node in message_nodes:
        try:
            if not isinstance(message_node, dict):
                continue

            sender = message_node.get("sender") or {}
            sender_name = (
                sender.get("nickname")
                or sender.get("card")
                or sender.get("user_id")
                or "未知用户"
            )

            raw_content = message_node.get("message") or message_node.get("content", [])

            content_chain: List[Any] = []
            if isinstance(raw_content, str):
                try:
                    parsed_content = json.loads(raw_content)
                    if isinstance(parsed_content, list):
                        content_chain = parsed_content
                except Exception:
                    content_chain = [{"type": "text", "data": {"text": raw_content}}]
            elif isinstance(raw_content, list):
                content_chain = raw_content

            node_text_parts: List[str] = []
            has_only_forward = False

            if isinstance(content_chain, list):
                first_seg = (
                    content_chain[0]
                    if len(content_chain) == 1 and isinstance(content_chain[0], dict)
                    else None
                )
                if first_seg and first_seg.get("type") == "forward":
                    has_only_forward = True

                for seg in content_chain:
                    if not isinstance(seg, dict):
                        continue
                    seg_type = seg.get("type")
                    seg_data = seg.get("data", {}) if isinstance(seg.get("data"), dict) else {}

                    if seg_type in ("text", "plain"):
                        text = seg_data.get("text", "")
                        if isinstance(text, str) and text:
                            node_text_parts.append(text)
                    elif seg_type == "image":
                        url = seg_data.get("url") or seg_data.get("file")
                        if isinstance(url, str) and url:
                            images.append(url)
                            node_text_parts.append("[图片]")
                    elif seg_type == "file":
                        node_text_parts.append("[文件]")
                    elif seg_type == "forward":
                        nested_content = seg_data.get("content")
                        if isinstance(nested_content, list):
                            _extract_forward_nodes_recursively(
                                nested_content, texts, images, depth + 1
                            )
                        else:
                            node_text_parts.append("[转发消息内容缺失或格式错误]")

            full_node_text = "".join(node_text_parts).strip()
            if full_node_text and not has_only_forward:
                texts.append(f"{indent}{sender_name}: {full_node_text}")
        except Exception as e:
            logger.debug(f"zssm_core: parse forward node failed: {e}")


def extract_from_onebot_forward_payload(payload: Any) -> Tuple[str, List[str]]:
    texts: List[str] = []
    images: List[str] = []
    data = ob_data(payload) if isinstance(payload, dict) else {}
    if isinstance(data, dict):
        msgs = (
            data.get("messages")
            or data.get("message")
            or data.get("nodes")
            or data.get("nodeList")
        )
        if isinstance(msgs, list):
            try:
                _extract_forward_nodes_recursively(msgs, texts, images, depth=0)
            except Exception as e:
                logger.debug(f"zssm_core: parse forward payload failed: {e}")
    return ("\n".join([x for x in texts if x]).strip(), images)


async def extract_quoted_payload(event: AstrMessageEvent) -> Tuple[Optional[str], List[str], bool]:
    try:
        chain = event.get_messages()
    except Exception:
        chain = getattr(event.message_obj, "message", []) or []

    reply_comp = None
    for seg in chain:
        try:
            if isinstance(seg, Comp.Reply):
                reply_comp = seg
                break
        except Exception:
            pass

    if not reply_comp:
        return (None, [], False)

    text, images, from_forward = try_extract_from_reply_component(reply_comp)
    if text or images:
        return (text, images, from_forward)

    for attr in ("message_str", "text"):
        try:
            v = getattr(reply_comp, attr, None)
            if isinstance(v, str) and v.strip():
                return (v.strip(), [], False)
        except Exception:
            continue

    reply_id = get_reply_message_id(reply_comp)
    if reply_id:
        try:
            ret = await cached_call_get_msg(event, reply_id)
            data = ob_data(ret or {})
            t2, imgs2 = extract_from_onebot_message_payload(data)
            agg_texts: List[str] = [t2] if t2 else []
            agg_imgs: List[str] = list(imgs2)
            from_forward_ob = False

            try:
                msg_list = data.get("message") if isinstance(data, dict) else None
                if isinstance(msg_list, list):
                    for seg in msg_list:
                        if not isinstance(seg, dict):
                            continue
                        seg_type = seg.get("type")
                        if seg_type in ("forward", "forward_msg", "nodes"):
                            from_forward_ob = True
                            d = seg.get("data", {}) if isinstance(seg.get("data"), dict) else {}
                            fid = d.get("id") or d.get("message_id")
                            if isinstance(fid, (str, int)) and str(fid):
                                try:
                                    fwd = await cached_call_get_forward_msg(event, str(fid))
                                    ft, fi = extract_from_onebot_forward_payload(fwd or {})
                                    if ft:
                                        agg_texts.append(ft)
                                    if fi:
                                        agg_imgs.extend(fi)
                                except Exception as fe:
                                    logger.warning(f"zssm_core: get_forward_msg failed: {fe}")
                        elif seg_type == "json":
                            try:
                                d = seg.get("data", {}) if isinstance(seg.get("data"), dict) else {}
                                inner_data_str = d.get("data")
                                if isinstance(inner_data_str, str) and inner_data_str.strip():
                                    inner_data_str = html.unescape(inner_data_str)
                                    inner_json = json.loads(inner_data_str)
                                    if (
                                        inner_json.get("app") == "com.tencent.multimsg"
                                        and inner_json.get("config", {}).get("forward") == 1
                                    ):
                                        detail = inner_json.get("meta", {}).get("detail", {}) or {}
                                        news_items = detail.get("news", []) or []
                                        for item in news_items:
                                            if not isinstance(item, dict):
                                                continue
                                            text_content = item.get("text")
                                            if isinstance(text_content, str):
                                                clean_text = text_content.strip().replace("[图片]", "").strip()
                                                if clean_text:
                                                    agg_texts.append(clean_text)
                                        if news_items:
                                            from_forward_ob = True
                            except Exception as je:
                                logger.debug(f"zssm_core: parse multimsg json in get_msg failed: {je}")
            except Exception:
                pass

            if agg_texts or agg_imgs:
                logger.info("zssm_core: fetched origin via get_msg")

                def _uniq(items: List[str]) -> List[str]:
                    uniq: List[str] = []
                    seen: set = set()
                    for it in items:
                        if isinstance(it, str) and it and it not in seen:
                            seen.add(it)
                            uniq.append(it)
                    return uniq

                return (
                    "\n".join([x for x in agg_texts if x]).strip(),
                    _uniq(agg_imgs),
                    from_forward_ob,
                )
        except Exception as e:
            logger.warning(f"zssm_core: get_msg failed: {e}")

    return (None, [], False)


def is_napcat(event: AstrMessageEvent) -> bool:
    try:
        if not (hasattr(event, "bot") and hasattr(event.bot, "api")):
            return False
        api = getattr(event.bot, "api", None)
        if api is None or not hasattr(api, "call_action"):
            return False
        return True
    except Exception:
        return False


async def napcat_resolve_file_url(event: AstrMessageEvent, file_id: str) -> Optional[str]:
    if not (isinstance(file_id, str) and file_id):
        return None
    if not is_napcat(event):
        return None

    try:
        gid = event.get_group_id()
    except Exception:
        gid = None

    group_id_param: Any = gid
    try:
        if isinstance(gid, str) and gid.isdigit():
            group_id_param = int(gid)
        elif isinstance(gid, int):
            group_id_param = gid
    except Exception:
        group_id_param = gid

    def _stem_if_needed(s: str) -> Optional[str]:
        try:
            base, ext = os.path.splitext(s)
            if ext and ext.lower() in (
                ".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tif", ".tiff", ".gif",
            ):
                if base and base != s:
                    return base
        except Exception:
            pass
        return None

    candidates: List[str] = [file_id]
    stem = _stem_if_needed(file_id)
    if isinstance(stem, str) and stem and stem not in candidates:
        candidates.append(stem)

    actions: List[Dict[str, Any]] = []
    for fid in candidates:
        actions.append({"action": "get_file", "params": {"file_id": fid}})
        actions.append({"action": "get_file", "params": {"file": fid}})
        actions.append({"action": "get_image", "params": {"file": fid}})
        actions.append({"action": "get_image", "params": {"file_id": fid}})
        actions.append({"action": "get_image", "params": {"id": fid}})
        actions.append({"action": "get_image", "params": {"image": fid}})

    if group_id_param:
        for fid in candidates:
            actions.append(
                {
                    "action": "get_group_file_url",
                    "params": {"group_id": group_id_param, "file_id": fid},
                }
            )
    for fid in candidates:
        actions.append({"action": "get_private_file_url", "params": {"file_id": fid}})

    for item in actions:
        action = item["action"]
        params = item["params"]
        try:
            ret = await event.bot.api.call_action(action, **params)
            if isinstance(ret, dict):
                d = ret.get("data")
                data = d if isinstance(d, dict) else ret
            else:
                data = None

            url = data.get("url") if isinstance(data, dict) else None
            if isinstance(url, str) and url:
                return url

            f = data.get("file") if isinstance(data, dict) else None
            if isinstance(f, str) and f:
                lf = f.lower()
                if lf.startswith("base64://") or lf.startswith("data:image/"):
                    return f
                if lf.startswith("file://"):
                    fp = f[7:]
                    if fp.startswith("/") and len(fp) > 3 and fp[2] == ":":
                        fp = fp[1:]
                    if fp and os.path.exists(fp):
                        return os.path.abspath(fp)
                if os.path.isabs(f) and os.path.exists(f):
                    return f
                if os.path.exists(f):
                    return os.path.abspath(f)
        except Exception:
            continue
    return None
