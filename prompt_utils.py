from __future__ import annotations

from typing import List, Optional


# === 默认提示词常量（集中管理，与 zssm_core 保持一致） ===

DEFAULT_SYSTEM_PROMPT = (
    "你是一个中文助理，负责解释用户提供或引用的内容。\n"
    "请严格按以下 Markdown 结构输出：\n\n"
    "**关键词**\n"
    "关键词1 | 关键词2 | 关键词3\n\n"
    "**详细阐述**\n"
    "排版要求（面向手机聊天界面，务必保证可读性）：\n"
    "1. 不要输出思考过程，不要输出多余小标题。\n"
    "2. 关键词放在“**关键词**”下一行，使用“ | ”分隔，数量不超过 6 个。\n"
    "3. 正文按 2~4 个小节分段撰写，节与节之间用空行分隔，每节围绕一个主题。\n"
    "4. 每段以短句为主，段落控制在 2~3 行，避免超长连续段落。\n"
    "5. 总篇幅控制在 400 字以内；信息过多时优先概括核心要点，不要罗列所有细节。\n"
)

# 短回复（100字内）- 用于“仅 zssm + 回复文件/消息”
DEFAULT_TEXT_USER_PROMPT_SHORT = (
    "请解释这条被回复的消息含义，回答简洁，不超过100字。\n"
    "原始文本：\n{text}"
)

DEFAULT_IMAGE_USER_PROMPT_SHORT = (
    "请解释这条被回复的消息/图片含义，回答简洁，不超过100字。\n"
    "{text_block}\n包含图片：请结合图片内容进行解释。"
)

# 长回复（不限字数）- 用于“zssm 问题”/“zssm 问题+文件”
DEFAULT_TEXT_USER_PROMPT_DETAIL = (
    "请详细解释下面内容，结合上下文给出完整说明，字数不限。\n"
    "原始文本：\n{text}"
)

DEFAULT_IMAGE_USER_PROMPT_DETAIL = (
    "请详细解释下面消息/图片内容，结合上下文给出完整说明，字数不限。\n"
    "{text_block}\n包含图片：请结合图片内容进行解释。"
)

# 在线搜索（grok2api 联网搜索 / X 搜索 / 组合搜索）
DEFAULT_SEARCH_SYSTEM_PROMPT = (
    "你是一个中文搜索助理。请基于联网搜索或 X（推特）搜索返回的最新信息回答用户问题，"
    "优先给出结论与关键事实，再按需补充背景。\n"
    "排版要求（面向手机聊天界面）：\n"
    "1. 使用简体中文，短句为主，段落控制在 2~3 行。\n"
    "2. 涉及时间敏感信息（新闻、行情、动态）时，注明信息的日期。\n"
    "3. 正文中用 [数字] 标注来源，例如 [1]，不要输出裸长链接。\n"
    "4. 总篇幅控制在 400 字以内；用户明确要求详细时除外。\n"
)

DEFAULT_SEARCH_USER_PROMPT = (
    "请联网搜索并回答下面的问题。\n"
    "问题：\n{text}"
)

DEFAULT_SEARCH_USER_PROMPT_X = (
    "请在 X（推特）上搜索并回答下面的问题，优先引用站内帖子与博主观点，并注明发帖时间。\n"
    "问题：\n{text}"
)

DEFAULT_SEARCH_USER_PROMPT_ALL = (
    "请同时使用联网搜索与 X（推特）搜索，综合网页与站内帖子信息回答下面的问题，"
    "注明信息来源与时间。\n"
    "问题：\n{text}"
)


def build_user_prompt(text: Optional[str], images: List[str], concise: bool = True) -> str:
    """根据是否包含图片、是否简短模式选择提示词模板。"""
    text_block = ("原始文本:\n" + text) if text else ""

    if concise:
        tmpl = DEFAULT_IMAGE_USER_PROMPT_SHORT if images else DEFAULT_TEXT_USER_PROMPT_SHORT
    else:
        tmpl = DEFAULT_IMAGE_USER_PROMPT_DETAIL if images else DEFAULT_TEXT_USER_PROMPT_DETAIL

    return tmpl.format(text=text or "", text_block=text_block)


def build_system_prompt() -> str:
    """返回系统提示词（供 LLM 调用使用）。"""
    return DEFAULT_SYSTEM_PROMPT


def build_search_user_prompt(text: Optional[str], search_kind: str = "web") -> str:
    """构建在线搜索场景的用户提示词。search_kind: web / x / all。"""
    if search_kind == "x":
        tmpl = DEFAULT_SEARCH_USER_PROMPT_X
    elif search_kind == "all":
        tmpl = DEFAULT_SEARCH_USER_PROMPT_ALL
    else:
        tmpl = DEFAULT_SEARCH_USER_PROMPT
    return tmpl.format(text=text or "")


def build_search_system_prompt() -> str:
    """返回在线搜索场景的系统提示词。"""
    return DEFAULT_SEARCH_SYSTEM_PROMPT
