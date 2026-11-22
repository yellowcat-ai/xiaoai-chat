"""本地曲库：文件名就是点播词，命中后完全不碰在线搜索"""
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

MUSIC_DIR = Path(__file__).with_name("local_music")
SUPPORTED = {".mp3", ".flac", ".wav"}

# 口语里常挂在歌名后面的词，匹配前剥掉一层
_TRAILING = ("这首歌", "那首歌", "这首", "那首", "歌曲", "儿歌", "的歌", "歌", "的")


def _norm(text: str) -> str:
    return "".join(text.lower().split())


def _entry(path: Path) -> dict:
    """文件名支持 "歌名 - 歌手" 的写法，没有歌手就取整个文件名"""
    title, sep, artist = path.stem.partition(" - ")
    return {
        "path": path,
        "name": title.strip(),
        "artist": artist.strip() if sep else "",
    }


def lookup(keyword: str) -> dict | None:
    """按文件名匹配；允许“贝乐虎儿歌的小兔子乖乖”这种带前缀的说法"""
    if not MUSIC_DIR.is_dir():
        return None
    key = _norm(keyword)
    if not key:
        return None
    forms = [key]
    for suffix in _TRAILING:
        if key.endswith(suffix) and len(key) > len(suffix):
            forms.append(key[:-len(suffix)])
            break

    best = None
    best_len = 0
    for path in MUSIC_DIR.iterdir():
        if not path.is_file() or path.suffix.lower() not in SUPPORTED:
            continue
        entry = _entry(path)
        title = _norm(entry["name"])
        if not title:
            continue
        if any(form == title or form.endswith(title) for form in forms) and len(title) > best_len:
            best, best_len = entry, len(title)
    return best
