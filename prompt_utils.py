from __future__ import annotations

from typing import Optional


# === 搜索场景提示词 ===

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


def build_search_user_prompt(text: Optional[str], search_x: bool = False) -> str:
    """构建搜索场景的用户提示词。"""
    tmpl = DEFAULT_SEARCH_USER_PROMPT_X if search_x else DEFAULT_SEARCH_USER_PROMPT
    return tmpl.format(text=text or "")


def build_search_system_prompt() -> str:
    """返回搜索场景的系统提示词。"""
    return DEFAULT_SEARCH_SYSTEM_PROMPT
