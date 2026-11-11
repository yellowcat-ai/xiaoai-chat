"""音乐搜索模块 - 基于 go-music-dl"""
import asyncio
import json
import logging
import re
import socket
from html.parser import HTMLParser
from urllib.parse import parse_qs, quote, urlencode, urlparse, urlunparse

import aiohttp

logger = logging.getLogger(__name__)


def _get_local_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"
    finally:
        s.close()


_local_ip = _get_local_ip()
MUSIC_DL_BASE = f"http://{_local_ip}:8080/music"
MUSIC_DL_LOCAL = "http://localhost:8080/music"


class SongCardParser(HTMLParser):
    """解析搜索结果 HTML 中的歌曲卡片"""

    def __init__(self):
        super().__init__()
        self.songs = []
        self.current_song = None
        self.in_song_card = False

    def handle_starttag(self, tag, attrs):
        if tag == "li" and any(cls == "song-card" for name, cls in attrs if name == "class"):
            self.in_song_card = True
            self.current_song = {}
            for name, value in attrs:
                if name.startswith("data-"):
                    key = name[5:].replace("-", "_")
                    self.current_song[key] = value or ""

    def handle_endtag(self, tag):
        if tag == "li" and self.in_song_card and self.current_song:
            self.in_song_card = False
            if self.current_song.get("id") and self.current_song.get("source"):
                self.songs.append(self.current_song)
            self.current_song = None


async def search_songs(keyword: str, source: str = "netease", max_results: int = 5) -> list[dict]:
    """搜索歌曲，返回歌曲列表"""
    url = f"{MUSIC_DL_LOCAL}/search?q={quote(keyword)}&source={source}&type=song&page=1"
    async with aiohttp.ClientSession() as session:
        try:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status != 200:
                    logger.debug(f"搜索 {source} 返回 {resp.status}")
                    return []
                html = await resp.text()
                parser = SongCardParser()
                parser.feed(html)
                # 过滤指定源
                songs = [s for s in parser.songs if s.get("source") == source]
                logger.info(f"搜索 '{keyword}' (源: {source}) 找到 {len(songs)} 首歌曲")
                return songs[:max_results]
        except Exception as e:
            logger.debug(f"搜索 {source} 失败：{e}")
            return []


_BRACKETS = re.compile(r"[（(\[【].*?[）)\]】]")
_VERSION_HINTS = (
    "伴奏", "伴唱", "翻唱", "cover", "live", "演唱会", "片段", "铃声", "dj", "remix",
    "纯音乐", "钢琴", "吉他", "口琴", "童声", "女声版", "男声版", "加速", "降调", "慢速",
)


def _norm(text: str) -> str:
    return "".join(ch for ch in text.lower() if not ch.isspace())


def _artist_votes(songs: list[dict]) -> dict[str, int]:
    """统计各源里出现频次高的歌手——原唱通常会在多个源反复出现"""
    votes: dict[str, int] = {}
    for song in songs:
        for artist in re.split(r"[/、,，&+\s]+", song.get("artist", "")):
            artist = artist.strip().lower()
            if len(artist) >= 2:
                votes[artist] = votes.get(artist, 0) + 1
    return votes


def _score(song: dict, keyword: str, votes: dict[str, int] | None = None) -> float:
    """给候选打分：标题贴近关键词、时长完整、歌手在多源高频出现（原唱）分越高"""
    name = song.get("name", "")
    artist = song.get("artist", "")
    norm_kw = _norm(keyword)
    norm_name = _norm(name)
    plain = _norm(_BRACKETS.sub("", name))

    score = 0.0
    if norm_name == norm_kw:
        score += 60
    elif plain == norm_kw:
        score += 40
    elif norm_kw and norm_kw in norm_name:
        score += 15
    else:
        score -= 20

    if _BRACKETS.search(name):
        score -= 10
    if any(hint in name.lower() for hint in _VERSION_HINTS):
        score -= 30

    try:
        duration = int(song.get("duration", 0))
    except (TypeError, ValueError):
        duration = 0
    if duration >= 200:
        score += 25
    elif duration >= 120:
        score += 10
    elif duration:
        score -= 15

    artists = [a.strip().lower() for a in re.split(r"[/、,，&+\s]+", artist) if len(a.strip()) >= 2]
    for token in re.split(r"[\s,，、/]+", keyword):
        if len(token) >= 2 and token.lower() in artist.lower():
            score += 30
    if votes and artists:
        # 取这首歌里最热门的那个歌手，避免合唱曲目把两个人的票叠加
        best = max(votes.get(a, 0) for a in artists)
        score += 12 * min(best, 4)
    return score


def rank_songs(songs: list[dict], keyword: str, votes: dict[str, int] | None = None) -> list[dict]:
    """按"像不像原版正曲"排序，同分保持搜索原始顺序"""
    return sorted(songs, key=lambda s: -_score(s, keyword, votes))


def build_stream_url(song: dict) -> str:
    """构建音频流 URL"""
    params = {
        "id": song.get("id", ""),
        "source": song.get("source", ""),
        "name": song.get("name", ""),
        "artist": song.get("artist", ""),
        "stream": "1",
    }

    album = song.get("album", "")
    if album:
        params["album"] = album

    cover = song.get("cover", "")
    if cover:
        params["cover"] = cover

    # 千千、汽水等源需要原样带回 extra 才能解析
    # 搜索结果里是 JSON 字符串，switch_source 返回的是对象，统一成 JSON 字符串
    extra = song.get("extra", "")
    if isinstance(extra, (dict, list)):
        extra = json.dumps(extra, ensure_ascii=False, separators=(",", ":"))
    if extra:
        params["extra"] = extra

    query_string = urlencode(params)
    return f"{MUSIC_DL_BASE}/download?{query_string}"


def source_of_url(url: str) -> str:
    """从下载地址里取出源名"""
    try:
        return parse_qs(urlparse(url).query).get("source", [""])[0]
    except Exception:
        return ""


def _artist_tokens(text: str) -> set[str]:
    return {a.strip().lower() for a in re.split(r"[/、,，&+\s]+", text or "") if len(a.strip()) >= 2}


async def switch_source(name: str, artist: str, source: str) -> dict | None:
    """当前源挂了，让 go-music-dl 在其它平台找同名同歌手的可播版本"""
    if not name or not source:
        return None
    params = {"name": name, "artist": artist, "source": source}
    async with aiohttp.ClientSession() as session:
        try:
            async with session.get(f"{MUSIC_DL_LOCAL}/switch_source", params=params,
                                   timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status != 200:
                    return None
                data = await resp.json(content_type=None)
        except Exception as e:
            logger.debug(f"换源失败: {name} - {e}")
            return None

        if not data.get("id") or not data.get("source") or data.get("source") == source:
            return None
        # 换源结果不一定靠谱，名字必须完全一致，歌手要有交集
        if _norm(data.get("name", "")) != _norm(name):
            logger.info(f"换源结果不是同一首，丢弃: {data.get('name')} - {data.get('artist')}")
            return None
        if artist and not (_artist_tokens(artist) & _artist_tokens(data.get("artist", ""))):
            logger.info(f"换源歌手不一致，丢弃: {data.get('name')} - {data.get('artist')}")
            return None
        if not await _song_playable(session, data):
            return None
        hit = await check_song_available(session, data)
        if not hit:
            return None
        return {"url": hit[0], "name": data.get("name"), "artist": data.get("artist")}


def prefer_mp3_url(url: str) -> str:
    """migu 的 id 里带音质段，改成 HQ 拿 MP3（FLAC 无法按帧切片）"""
    parsed = urlparse(url)
    qs = parse_qs(parsed.query)
    song_id = qs.get("id", [""])[0]
    if qs.get("source", [""])[0] == "migu" and song_id.count("|") == 2:
        content_id, resource_type, _ = song_id.split("|")
        qs["id"] = [f"{content_id}|{resource_type}|HQ"]
        return urlunparse(parsed._replace(query=urlencode(qs, doseq=True)))
    return url


async def _inspect_song(session: aiohttp.ClientSession, song: dict) -> dict | None:
    """调 go-music-dl 的 inspect，拿到解析结果（是否可用、是否只有试听片段）

    v1.1.4 起提供；旧版本或接口异常时返回 None，退回原来的探测逻辑。
    """
    try:
        params = {"id": song.get("id", ""), "source": song.get("source", "")}
        async with session.get(f"{MUSIC_DL_LOCAL}/inspect", params=params,
                               timeout=aiohttp.ClientTimeout(total=3)) as resp:
            if resp.status != 200:
                return None
            return await resp.json(content_type=None)
    except Exception as e:
        logger.debug(f"inspect 失败: {song.get('name')} - {e}")
        return None


async def _probe_type(session: aiohttp.ClientSession, url: str) -> str | None:
    """取 1 个字节，返回 Content-Type（小写）"""
    try:
        async with session.get(url, headers={"Range": "bytes=0-0"},
                               timeout=aiohttp.ClientTimeout(total=5)) as resp:
            if resp.status in (200, 206):
                return resp.headers.get("Content-Type", "").lower()
    except Exception as e:
        logger.debug(f"探测音频失败: {url[:60]} - {e}")
    return None


def _ctype_is_mp3(ctype: str) -> bool:
    return "mpeg" in ctype or "mp3" in ctype


async def check_song_available(session: aiohttp.ClientSession, song: dict) -> tuple[str, str, str, bool] | None:
    """检查歌曲是否可用，返回 (url, name, source, 是否 MP3)"""
    url = prefer_mp3_url(build_stream_url(song))
    name = f"{song.get('name', '')} - {song.get('artist', '')}"
    source = song.get('source', '')

    ctype = await _probe_type(session, url)
    if not ctype:
        return None
    return url, name, source, _ctype_is_mp3(ctype)


async def _song_playable(session: aiohttp.ClientSession, song: dict) -> bool:
    """选中的候选再确认一次：源解析成功、不是只有试听片段

    放在选定之后调用，避免给每个候选都多发一次请求。
    """
    info = await _inspect_song(session, song)
    if info is None:
        return True  # 旧版本或接口异常，按原来的逻辑放行
    name = f"{song.get('name', '')} - {song.get('artist', '')}"
    if info.get("status") != "valid":
        logger.info(f"源解析失败，跳过: {name} (源: {song.get('source')})")
        return False
    if info.get("preview_only"):
        logger.info(f"只有试听片段，跳过: {name} (源: {song.get('source')})")
        return False
    return True


async def _check_group(songs: list[dict]) -> tuple[tuple[str, str, str, bool] | None, tuple[str, str, str, bool] | None]:
    """检查一组歌曲，返回 (首选 MP3 候选, 任意可播候选)"""
    async with aiohttp.ClientSession() as session:
        results = await asyncio.gather(*[check_song_available(session, s) for s in songs], return_exceptions=True)

        usable = []
        for song, result in zip(songs, results):
            if isinstance(result, Exception) or result is None:
                continue
            usable.append((song, result))
        # MP3 优先，再按原来的候选顺序
        usable.sort(key=lambda item: not item[1][3])

        # 先只确认 MP3 候选，避免为整条兜底链都多发 inspect 请求
        for song, result in usable:
            if result[3] and await _song_playable(session, song):
                return result, None
        for song, result in usable:
            if not result[3] and await _song_playable(session, song):
                return None, result
        return None, None


def _artist_list(song: dict) -> list[str]:
    return [a.strip().lower() for a in re.split(r"[/、,，&+\s]+", song.get("artist", "")) if len(a.strip()) >= 2]


def _pool_confident(songs: list[dict], keyword: str) -> bool:
    """候选里已经有一首足够像原唱的，就不必再等慢源"""
    if not songs:
        return False
    votes = _artist_votes(songs)
    top = rank_songs(songs, keyword, votes)[0]
    name = top.get("name", "")
    if _norm(name) != _norm(keyword) or _BRACKETS.search(name):
        return False
    if any(hint in name.lower() for hint in _VERSION_HINTS):
        return False
    try:
        if 0 < int(top.get("duration", 0)) < 180:
            return False
    except (TypeError, ValueError):
        pass
    return max((votes.get(a, 0) for a in _artist_list(top)), default=0) >= 2


def _early_candidate(songs: list[dict], keyword: str) -> list[dict] | None:
    """候选是否足够靠谱到可以提前开播：标题完全一致、不是 Live/翻唱等版本、歌手已有多源印证"""
    if not _pool_confident(songs, keyword):
        return None
    return rank_songs(songs, keyword, _artist_votes(songs))


async def search_and_play(keyword: str, source: str = "netease") -> tuple[str, str] | None:
    """并发搜索所有源；出现高置信候选就提前开播，4 秒后还不够靠谱就再等 3 秒慢源"""
    sources = ["migu", "netease", "qq", "kugou", "qianqian", "kuwo"]  # 咪咕打平时优先

    logger.info(f"并发搜索 {len(sources)} 个源 (优先级: {', '.join(sources)})...")

    tasks = [asyncio.create_task(search_songs(keyword, source=src, max_results=8)) for src in sources]
    results: dict[str, list[dict]] = {}
    loop = asyncio.get_running_loop()
    deadline = loop.time() + 4.0
    hard_deadline = deadline + 3.0

    def collect(finished):
        for src, task in zip(sources, tasks):
            if task in finished and not task.cancelled() and task.exception() is None:
                songs = task.result() or []
                if songs:
                    results[src] = songs

    try:
        pending = set(tasks)
        early_fallback = None
        early_checked = False
        while pending and loop.time() < hard_deadline:
            if loop.time() >= deadline:
                # 已过基础等待时间：候选够靠谱就直接收工，否则再给慢源一点时间
                pool_now = [s for songs in results.values() for s in songs]
                if _pool_confident(pool_now, keyword):
                    break
            done, pending = await asyncio.wait(pending, timeout=0.4)
            collect(done)
            if loop.time() >= deadline:
                continue
            if early_checked or len(results) < 3:
                continue
            early_checked = True
            early = _early_candidate([s for songs in results.values() for s in songs], keyword)
            if not early:
                continue
            mp3_hit, early_fallback = await _check_group(early[:3])
            if mp3_hit:
                url, name, src_name, _ = mp3_hit
                logger.info(f"提前命中 MP3 歌曲: {name} (源: {src_name})")
                return url, name
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()

    if not results:
        logger.warning(f"所有源都未找到歌曲: {keyword}")
        return None

    # 跨源汇总后统一打分：原唱在多个源反复出现，分数自然更高
    pool = [song for songs in results.values() for song in songs]
    votes = _artist_votes(pool)
    candidates = []
    for src in sources:
        for song in results.get(src, []):
            candidates.append((-_score(song, keyword, votes), sources.index(src), song))
    candidates.sort(key=lambda item: (item[0], item[1]))

    # 全都没沾上标题/歌手（比如搜不存在的歌名时源站的模糊结果），宁可不放
    if -candidates[0][0] < 30:
        logger.warning(f"候选和关键词都不匹配，放弃: {keyword}")
        return None

    logger.info(f"候选 {len(candidates)} 首，最优: {candidates[0][2].get('name')} - {candidates[0][2].get('artist')}")

    fallback = early_fallback  # 非 MP3 的可播候选，最后兜底
    for start in range(0, len(candidates), 8):
        batch = [item[2] for item in candidates[start:start + 8]]
        mp3_hit, group_fallback = await _check_group(batch)
        if mp3_hit:
            url, name, source_name, _ = mp3_hit
            logger.info(f"找到可用 MP3 歌曲: {name} (源: {source_name})")
            return url, name
        if group_fallback and fallback is None:
            fallback = group_fallback

    if fallback:
        url, name, source_name, _ = fallback
        logger.warning(f"只找到非 MP3 版本，将转码后分段播放: {name} (源: {source_name})")
        return url, name

    logger.warning(f"所有源都未找到可用歌曲: {keyword}")
    return None


_SOURCE_PRIORITY = ["migu", "kuwo", "qq", "kugou", "netease", "qianqian"]


async def _pick_mp3(candidates: list[tuple[str, dict]]) -> tuple[str, dict] | None:
    """并行探测同一首歌的多个源，返回第一个 MP3 版本（按源优先级）"""
    if not candidates:
        return None
    ordered = sorted(candidates, key=lambda c: _SOURCE_PRIORITY.index(c[0])
                     if c[0] in _SOURCE_PRIORITY else 99)
    async with aiohttp.ClientSession() as session:
        checks = await asyncio.gather(*[
            check_song_available(session, song) for _, song in ordered
        ], return_exceptions=True)
        # 校验必须在 session 关闭前完成
        for (src, song), hit in zip(ordered, checks):
            if isinstance(hit, Exception) or not hit:
                continue
            if hit[3] and await _song_playable(session, song):
                return hit[0], song
    return None


async def search_artist_tracks(artist: str, limit: int = 8) -> list[tuple[str, str]]:
    """多源找歌手的正曲，只留 MP3 可直接切片的版本"""
    sources = ["migu", "kuwo", "qq", "kugou", "netease", "qianqian"]
    raw = await asyncio.gather(*[
        search_songs(artist, source=src, max_results=20) for src in sources
    ], return_exceptions=True)

    low = artist.lower()
    groups: dict[str, list[tuple[str, dict]]] = {}
    src_count: dict[str, set[str]] = {}
    for src, songs in zip(sources, raw):
        if isinstance(songs, Exception) or not songs:
            continue
        for song in songs:
            name = song.get("name", "")
            if low not in (song.get("artist") or "").lower():
                continue
            if _BRACKETS.search(name) or any(hint in name.lower() for hint in _VERSION_HINTS):
                continue
            try:
                if int(song.get("duration", 0)) < 90:
                    continue
            except (TypeError, ValueError):
                pass
            key = _norm(name)
            groups.setdefault(key, []).append((src, song))
            src_count.setdefault(key, set()).add(src)

    if not groups:
        return []

    # 多源都收录的更像正曲，其次看时长
    def rank_item(key: str):
        songs = [s for _, s in groups[key]]
        try:
            dur = max(int(s.get("duration", 0)) for s in songs)
        except (TypeError, ValueError):
            dur = 0
        return (-len(src_count[key]), -dur)

    keys = sorted(groups, key=rank_item)[:limit * 2]
    picks = await asyncio.gather(*[_pick_mp3(groups[k]) for k in keys], return_exceptions=True)

    tracks: list[tuple[str, str]] = []
    for hit in picks:
        if isinstance(hit, Exception) or not hit:
            continue
        url, song = hit
        tracks.append((url, f"{song.get('name', '')} - {song.get('artist', '')}"))
        if len(tracks) >= limit:
            break
    return tracks
