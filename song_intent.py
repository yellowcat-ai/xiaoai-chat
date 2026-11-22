"""用大模型把“灌篮高手主题曲”这类描述改写成正式歌名"""

import asyncio
import json
import logging
import re
from pathlib import Path

import config
from llm_client import client

logger = logging.getLogger(__name__)

CACHE_FILE = Path(__file__).with_name("song_intent_cache.json")
_MAX_ENTRIES = 500

# 只有带这些说法、或者长到不像歌名的请求才值得花一次大模型调用
_DESCRIPTION_HINTS = (
    "主题曲", "主题歌", "主题", "片头曲", "片尾曲", "片头", "片尾", "插曲",
    "背景音乐", "bgm", "动画", "动漫", "电影", "电视剧", "游戏",
    "那首", "那个", "里面", "里的", "唱的", "原唱", "儿歌", "童谣", "唱的那首",
)

_SYSTEM_PROMPT = (
    "你是点歌请求解析器。用户说出的是点歌请求，可能是描述而不是正式歌名。"
    "请给出它对应的正式歌曲名，知道原唱时一并给出歌手。"
    "如果用户说的本来就是正式歌名，原样返回；无法确定具体歌曲时 title 留空。\n"
    "示例：\n"
    "迪迦奥特曼主题曲 -> {\"title\": \"奇迹再现\", \"artist\": \"毛华锋\"}\n"
    "灌篮高手主题曲 -> {\"title\": \"好想大声说爱你\", \"artist\": \"BAAD\"}\n"
    "火影忍者主题曲 -> {\"title\": \"青鸟\", \"artist\": \"いきものがかり\"}\n"
    "孤勇者 -> {\"title\": \"孤勇者\", \"artist\": \"陈奕迅\"}\n"
    "只输出 JSON，不要解释。"
)

_cache: dict[str, dict] = {}


def _load_cache() -> None:
    try:
        data = json.loads(CACHE_FILE.read_text("utf-8"))
    except FileNotFoundError:
        return
    except Exception as e:
        logger.warning(f"读取歌名推断缓存失败: {e}")
        return
    if isinstance(data, dict):
        _cache.update({str(k): v for k, v in data.items() if isinstance(v, dict)})


def _save_cache() -> None:
    try:
        tmp = CACHE_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(_cache, ensure_ascii=False, indent=1), "utf-8")
        tmp.replace(CACHE_FILE)
    except Exception as e:
        logger.warning(f"保存歌名推断缓存失败: {e}")


def is_descriptive(keyword: str) -> bool:
    low = keyword.lower()
    if any(hint in low for hint in _DESCRIPTION_HINTS):
        return True
    return len("".join(keyword.split())) >= 9


def _remember(key: str, value: dict) -> None:
    if len(_cache) >= _MAX_ENTRIES:
        for old in list(_cache)[: len(_cache) - _MAX_ENTRIES + 1]:
            _cache.pop(old, None)
    _cache[key] = value
    _save_cache()


async def infer(keyword: str, descriptive_only: bool = True) -> dict | None:
    """点歌请求 -> {"title", "artist"}；推断失败返回 None

    descriptive_only=False 时普通歌名也会问一次，主要用于拿到原唱歌手。
    """
    key = keyword.strip()
    if not key or (descriptive_only and not is_descriptive(key)):
        return None
    if key in _cache:
        cached = _cache[key]
        return cached if cached.get("title") else None

    try:
        resp = await asyncio.wait_for(
            client.chat.completions.create(
                model=config.LLM_MODEL,
                messages=[
                    {"role": "system", "content": _SYSTEM_PROMPT},
                    {"role": "user", "content": key},
                ],
                temperature=0,
                max_tokens=60,
            ),
            timeout=5,
        )
        text = (resp.choices[0].message.content or "").strip()
    except Exception as e:
        logger.debug(f"歌名推断失败: {key} - {e}")
        return None

    m = re.search(r"\{.*\}", text, re.S)
    try:
        data = json.loads(m.group(0)) if m else {}
    except json.JSONDecodeError:
        data = {}

    title = str(data.get("title") or "").strip()
    artist = str(data.get("artist") or "").strip()
    if not title or len(title) > 30:
        _remember(key, {})
        return None

    result = {"title": title, "artist": artist}
    _remember(key, result)
    if title != key:
        logger.info(f"歌名推断：{key} → {title}" + (f" - {artist}" if artist else ""))
    return result


_load_cache()