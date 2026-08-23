from __future__ import annotations

import re


# === 回复格式化：把 LLM 的 markdown 降级为 QQ 等纯文本客户端友好的格式（与 zssm_core 一致） ===

# 相邻 markdown 链接之间补空格，避免 QQ 等客户端把紧邻的两条 URL 合并识别
MARKDOWN_LINK_JOIN_PATTERN = re.compile(r"\]\((https?://[^)\s]+)\)(?=\[)", re.I)
# 将 [[n]](url) -> [n] url；[text](url) -> text（url）
MARKDOWN_REF_LINK_PATTERN = re.compile(r"\[\[(\d+)\]\]\(\s*(https?://[^)\s]+)\s*\)", re.I)
MARKDOWN_LINK_PATTERN = re.compile(r"\[([^\]]+)\]\(\s*(https?://[^)\s]+)\s*\)", re.I)
MARKDOWN_BOLD_PATTERN = re.compile(r"\*\*([^*]+)\*\*")
MARKDOWN_STRIKE_PATTERN = re.compile(r"~~([^~]+)~~")
MARKDOWN_INLINE_CODE_PATTERN = re.compile(r"`([^`]+)`")
MARKDOWN_ITALIC_PATTERN = re.compile(r"(?<!\*)\*([^*\n]+)\*(?!\*)")
MARKDOWN_HEADING_PATTERN = re.compile(r"^\s{0,3}#{1,6}\s+(.*)$", re.M)
MARKDOWN_BLOCK_CODE_PATTERN = re.compile(r"```[a-zA-Z0-9_+\-.]*\s*\n(.*?)```", re.S)


def normalize_link_spacing(text: str) -> str:
    """在相邻的 markdown 链接之间补一个空格，避免 QQ 等客户端把紧邻的两条 URL 合并识别。"""
    if not isinstance(text, str) or not text:
        return text
    return MARKDOWN_LINK_JOIN_PATTERN.sub(r"](\1) ", text)


def demote_markdown_to_text(text: str) -> str:
    """把 LLM 输出的 markdown 降级为适合 QQ 等纯文本客户端的格式：
    去掉 ** 加粗、斜体、~~删除线~~、inline code、标题 # 与代码块围栏；
    [[n]](url) 转为 [n] url、[text](url) 转为 text（url），裸 URL 由 QQ 自动识别为可点击链接；
    保留固定小节标题 **关键词** / **详细阐述** 的加粗标记。"""
    if not isinstance(text, str) or not text:
        return text
    t = text
    t = MARKDOWN_BLOCK_CODE_PATTERN.sub(lambda m: "\n" + m.group(1).strip("\n") + "\n", t)
    # 保留固定标题 **关键词** / **详细阐述** 的加粗标记，其余 ** 标记照常剥离
    t = t.replace("**关键词**", "\x00ZSSM_KEEP_KW\x00").replace("**详细阐述**", "\x00ZSSM_KEEP_EL\x00")
    t = MARKDOWN_REF_LINK_PATTERN.sub(lambda m: f"[{m.group(1)}] {m.group(2)}", t)
    t = MARKDOWN_LINK_PATTERN.sub(lambda m: f"{m.group(1)}（{m.group(2)}）", t)
    t = MARKDOWN_BOLD_PATTERN.sub(r"\1", t)
    t = MARKDOWN_STRIKE_PATTERN.sub(r"\1", t)
    t = MARKDOWN_INLINE_CODE_PATTERN.sub(r"\1", t)
    t = MARKDOWN_ITALIC_PATTERN.sub(r"\1", t)
    t = MARKDOWN_HEADING_PATTERN.sub(r"\1", t)
    t = t.replace("\x00ZSSM_KEEP_KW\x00", "**关键词**").replace("\x00ZSSM_KEEP_EL\x00", "**详细阐述**")
    t = re.sub(r"\n{3,}", "\n\n", t)
    return t
