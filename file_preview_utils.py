from __future__ import annotations

from typing import Iterable, Optional, Set, Dict, Any, List
import os
import io
import re
import asyncio

try:
    import aiohttp  # type: ignore[import-not-found]
except Exception:  # pragma: no cover
    aiohttp = None

try:
    import fitz  # type: ignore[import-not-found]
except Exception:  # pragma: no cover
    fitz = None  # type: ignore[assignment]

try:
    import PyPDF2  # type: ignore[import-not-found]
except Exception:  # pragma: no cover
    PyPDF2 = None  # type: ignore[assignment]

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent
import astrbot.api.message_components as Comp

from .message_utils import (
    get_reply_message_id,
    ob_data,
    cached_call_get_forward_msg,
    cached_call_get_msg,
)


def build_text_exts_from_config(raw: str, default_exts: Iterable[str]) -> Set[str]:
    base: Set[str] = set()
    for ext in default_exts:
        e = str(ext).strip().lower()
        if not e:
            continue
        if not e.startswith("."):
            e = "." + e
        base.add(e)
    if not isinstance(raw, str) or not raw.strip():
        return base
    for part in raw.split(","):
        p = part.strip().lower()
        if not p:
            continue
        if not p.startswith("."):
            p = "." + p
        base.add(p)
    return base


def _normalize_pdf_page_text(raw: Optional[str]) -> str:
    if not isinstance(raw, str):
        return ""
    lines = [ln.rstrip() for ln in raw.splitlines()]
    blocks: List[str] = []
    current: List[str] = []

    bullet_pattern = re.compile(r"^(\s*[-*•·]\s+|\s*\d{1,3}[.)]\s+)")

    def flush_paragraph() -> None:
        if not current:
            return
        joined = " ".join(s.strip() for s in current if s.strip())
        if joined:
            blocks.append(joined)
        current.clear()

    for line in lines:
        s = line.strip()
        if not s:
            flush_paragraph()
            continue
        if bullet_pattern.match(s):
            flush_paragraph()
            blocks.append(s)
            continue
        current.append(s)

    flush_paragraph()
    return "\n\n".join(blocks).strip()


def _pdf_bytes_to_markdown_sync(data: bytes, max_pages: Optional[int] = None) -> str:
    """同步执行的 PDF 解析逻辑，应在线程池中运行。"""
    if not data:
        return ""

    if fitz is not None:
        try:
            doc = fitz.open(stream=data, filetype="pdf")  # type: ignore[arg-type]
            page_count = int(getattr(doc, "page_count", len(doc)))  # type: ignore[arg-type]
            md_pages: List[str] = []
            for idx in range(page_count):
                page_no = idx + 1
                if isinstance(max_pages, int) and max_pages > 0 and page_no > max_pages:
                    break
                try:
                    page = doc.load_page(idx)
                    md = page.get_text("markdown") or page.get_text("text")
                except Exception:
                    md = ""
                if isinstance(md, str):
                    md = md.strip()
                if md:
                    md_pages.append(f"### 第 {page_no} 页\n\n{md}")
            if md_pages:
                return "\n\n---\n\n".join(md_pages).strip()
        except Exception as e:
            logger.warning(f"zssm_core: PyMuPDF markdown extract failed: {e}")

    if PyPDF2 is None:
        return ""
    try:
        reader = PyPDF2.PdfReader(io.BytesIO(data))  # type: ignore[call-arg]
    except Exception as e:
        logger.warning(f"zssm_core: pdf read failed: {e}")
        return ""

    md_pages_fallback: List[str] = []
    for idx, page in enumerate(reader.pages, start=1):
        if isinstance(max_pages, int) and max_pages > 0 and idx > max_pages:
            break
        try:
            raw = page.extract_text()  # type: ignore[call-arg]
        except Exception:
            raw = None
        text = _normalize_pdf_page_text(raw)
        if text:
            md_pages_fallback.append(f"### 第 {idx} 页\n\n{text}")
    return "\n\n---\n\n".join(md_pages_fallback).strip()


async def pdf_bytes_to_markdown(data: bytes, max_pages: Optional[int] = None) -> str:
    """异步封装的 PDF 解析。"""
    if not data:
        return ""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, _pdf_bytes_to_markdown_sync, data, max_pages)


def _find_first_file_in_message_list(msg_list: List[Any]) -> Optional[Dict[str, Any]]:
    if not isinstance(msg_list, list):
        return None
    for seg in msg_list:
        try:
            if not isinstance(seg, dict):
                continue
            t = seg.get("type")
            d = seg.get("data") if isinstance(seg.get("data"), dict) else {}
            if t == "file" and isinstance(d, dict):
                return seg
            content = seg.get("content") or seg.get("message")
            if isinstance(content, list):
                inner = _find_first_file_in_message_list(content)
                if inner is not None:
                    return inner
        except Exception:
            continue
    return None


def _find_first_file_in_forward_payload(payload: Any) -> Optional[Dict[str, Any]]:
    data = ob_data(payload) if isinstance(payload, dict) else {}
    if not isinstance(data, dict):
        return None
    msgs = data.get("messages") or data.get("message") or data.get("nodes") or data.get("nodeList")
    if not isinstance(msgs, list):
        return None
    for node in msgs:
        try:
            content = None
            if isinstance(node, dict):
                content = node.get("content") or node.get("message")
            if isinstance(content, list):
                seg = _find_first_file_in_message_list(content)
                if seg is not None:
                    return seg
        except Exception:
            continue
    return None


async def extract_file_preview_from_reply(
    event: AstrMessageEvent,
    text_exts: Set[str],
    max_size_bytes: Optional[int] = None,
    download_max_bytes: Optional[int] = None,
    text_max_chars: int = 400,
) -> Optional[str]:
    try:
        platform = event.get_platform_name()
    except Exception:
        platform = None
    if platform != "aiocqhttp" or not hasattr(event, "bot"):
        return None

    try:
        chain = event.get_messages()
    except Exception:
        chain = getattr(event.message_obj, "message", []) if hasattr(event, "message_obj") else []
    reply_comp = None
    for seg in chain:
        try:
            if isinstance(seg, Comp.Reply):
                reply_comp = seg
                break
        except Exception:
            continue
    if not reply_comp:
        return None

    reply_id = get_reply_message_id(reply_comp)
    if not reply_id:
        return None

    try:
        ret = await cached_call_get_msg(event, reply_id)
    except Exception:
        return None
    data = ob_data(ret or {}) if isinstance(ret, dict) else {}
    if not isinstance(data, dict):
        return None
    msg_list = data.get("message") or data.get("messages")
    if not isinstance(msg_list, list):
        return None

    file_seg = _find_first_file_in_message_list(msg_list)

    if not file_seg:
        for seg in msg_list:
            try:
                if not isinstance(seg, dict):
                    continue
                t = seg.get("type")
                if t not in ("forward", "forward_msg", "nodes"):
                    continue
                d = seg.get("data") if isinstance(seg.get("data"), dict) else {}
                fid = d.get("id")
                if not isinstance(fid, str) or not fid:
                    continue
                try:
                    fwd = await cached_call_get_forward_msg(event, str(fid))
                except Exception as fe:
                    logger.warning(f"zssm_core: get_forward_msg for file preview failed: {fe}")
                    continue
                inner = _find_first_file_in_forward_payload(fwd)
                if inner is not None:
                    file_seg = inner
                    break
            except Exception:
                continue

    if not file_seg:
        return None

    d = file_seg.get("data") or {}
    if not isinstance(d, dict):
        return None
    file_id = d.get("file")
    file_name = d.get("name") or d.get("file") or "未命名文件"
    summary = d.get("summary") or ""
    if not isinstance(file_id, str) or not file_id:
        return None

    return await build_group_file_preview(
        event=event,
        file_id=file_id,
        file_name=file_name,
        summary=summary,
        text_exts=text_exts,
        max_size_bytes=max_size_bytes,
        download_max_bytes=download_max_bytes,
        text_max_chars=text_max_chars,
    )


async def build_group_file_preview(
    event: AstrMessageEvent,
    file_id: str,
    file_name: str,
    summary: str,
    text_exts: Set[str],
    max_size_bytes: Optional[int] = None,
    download_max_bytes: Optional[int] = None,
    text_max_chars: int = 400,
) -> Optional[str]:
    try:
        gid = event.get_group_id()
    except Exception:
        gid = None

    url: Optional[str] = None

    if gid:
        try:
            group_id = int(gid)
        except Exception:
            group_id = None
        if group_id is not None:
            try:
                url_result = await event.bot.api.call_action(
                    "get_group_file_url",
                    group_id=group_id,
                    file_id=file_id,
                )
                url = url_result.get("url") if isinstance(url_result, dict) else None
            except Exception as e:
                logger.warning(f"zssm_core: get_group_file_url failed: {e}")

    if not url:
        try:
            url_result = await event.bot.api.call_action("get_private_file_url", file_id=file_id)
            data = url_result.get("data") if isinstance(url_result, dict) else None
            if isinstance(data, dict):
                url = data.get("url")
        except Exception as e:
            logger.warning(f"zssm_core: get_private_file_url failed: {e}")

    meta_lines: List[str] = [f"[群文件] {file_name}"]
    if summary:
        meta_lines.append(f"说明: {summary}")

    if not url or aiohttp is None:
        return "\n".join(meta_lines)

    name_lower = str(file_name).lower()
    _, ext = os.path.splitext(name_lower)
    is_pdf = ext == ".pdf"
    if ext not in text_exts and not is_pdf:
        return "\n".join(meta_lines)

    snippet = ""
    size_hint = ""
    chars_budget = max(80, int(text_max_chars)) if isinstance(text_max_chars, int) else 400
    dl_budget = (
        int(download_max_bytes)
        if isinstance(download_max_bytes, int) and download_max_bytes > 0
        else (2 * 1024 * 1024 if is_pdf else 4096)
    )

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=15) as resp:
                if resp.status != 200:
                    logger.warning("zssm_core: fetch group file failed, status=%s", resp.status)
                else:
                    cl = resp.headers.get("Content-Length")
                    if cl and cl.isdigit():
                        sz = int(cl)
                        if sz < 1024:
                            size_hint = f"{sz} B"
                        elif sz < 1024 * 1024:
                            size_hint = f"{sz / 1024:.1f} KB"
                        else:
                            size_hint = f"{sz / 1024 / 1024:.2f} MB"

                        if (
                            not is_pdf
                            and isinstance(max_size_bytes, int)
                            and max_size_bytes > 0
                            and sz > max_size_bytes
                        ):
                            meta_lines.append(f"大小: {size_hint}")
                            meta_lines.append("（文件体积较大，已跳过内容预览，仅展示元信息）")
                            return "\n".join(meta_lines)

                    if is_pdf and (fitz is not None or PyPDF2 is not None):
                        buf = io.BytesIO()
                        total = 0
                        truncated_by_dl = False
                        async for chunk in resp.content.iter_chunked(8192):
                            if not chunk:
                                break
                            total += len(chunk)
                            if total > dl_budget:
                                remain = dl_budget - (total - len(chunk))
                                if remain > 0:
                                    buf.write(chunk[:remain])
                                truncated_by_dl = True
                                break
                            buf.write(chunk)

                        pdf_bytes = buf.getvalue()
                        try:
                            text = await pdf_bytes_to_markdown(pdf_bytes)
                        except Exception as e:
                            logger.warning(f"zssm_core: pdf text extract failed: {e}")
                            text = ""

                        if text:
                            if isinstance(max_size_bytes, int) and max_size_bytes > 0:
                                try:
                                    txt_bytes = len(text.encode("utf-8", errors="ignore"))
                                except Exception:
                                    txt_bytes = len(text)
                                if txt_bytes > max_size_bytes:
                                    if size_hint:
                                        meta_lines.append(f"大小: {size_hint}")
                                    meta_lines.append("（PDF 文本内容较长，已跳过内容预览，仅展示元信息）")
                                    return "\n".join(meta_lines)

                            snippet = text if len(text) <= chars_budget else (text[:chars_budget] + " ...")
                            if truncated_by_dl:
                                meta_lines.append("（PDF 下载达到预览预算，内容可能不完整）")
                    else:
                        data = await resp.content.read(dl_budget)
                        try:
                            text = data.decode("utf-8", errors="ignore")
                        except Exception:
                            text = ""
                        text = text.strip()
                        if text:
                            snippet = text if len(text) <= chars_budget else (text[:chars_budget] + " ...")

    except Exception as e:
        logger.warning(f"zssm_core: preview group file content failed: {e}")

    if size_hint:
        meta_lines.append(f"大小: {size_hint}")
    if snippet:
        meta_lines.append("内容片段（截取部分，可能不完整）:")
        meta_lines.append(snippet)

    return "\n".join(meta_lines)
