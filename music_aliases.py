"""点歌别名：把“迪迦奥特曼主题曲”这类描述改写成正式歌名"""
import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

ALIAS_FILE = Path(__file__).with_name("music_aliases.json")


def _load() -> dict[str, str]:
    try:
        data = json.loads(ALIAS_FILE.read_text("utf-8"))
    except FileNotFoundError:
        return {}
    except Exception as e:
        logger.warning(f"读取点歌别名失败: {e}")
        return {}
    return {str(k).strip(): str(v).strip() for k, v in data.items() if str(k).strip() and str(v).strip()}


def resolve(keyword: str) -> str | None:
    """完全匹配优先，其次取查询里出现的最长别名"""
    aliases = _load()
    key = keyword.strip()
    if key in aliases:
        return aliases[key]
    best_key = ""
    for alias in aliases:
        if alias in key and len(alias) > len(best_key):
            best_key = alias
    return aliases[best_key] if best_key else None