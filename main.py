import asyncio
import logging
import logging.handlers
import re
import ssl
from pathlib import Path
from time import time

# Python 3.14 SSL 验证更严格，绕过证书问题
ssl._create_default_https_context = ssl._create_unverified_context

import config
from xiaoai import create_speakers, XiaoAiSpeaker
from llm_client import chat
from tts_engine import generate_tts, get_silence_url, wait_fetch
from music_search import search_and_play, search_artist_tracks, prefer_mp3_url, switch_source, source_of_url
from segmented_push import (
    fetch_audio,
    fetch_audio_head,
    is_mp3,
    iter_mp3_chunks,
    stream_mp3_chunks,
    transcode_to_mp3,
    write_chunk,
    cleanup_chunks,
)
import song_cache

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.handlers.RotatingFileHandler(
            "xiaoai.log", maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"
        ),
    ],
)
logger = logging.getLogger(__name__)


async def main():
    print("=" * 50)
    print("  小爱音箱 × 大模型对话")
    print("=" * 50)
    print()

    if not config.MI_USER or not config.MI_PASS:
        print("错误：请在 .env 文件中配置 MI_USER 和 MI_PASS")
        return
    if not config.LLM_API_KEY:
        print("错误：请在 .env 文件中配置 LLM_API_KEY")
        return

    speakers = await create_speakers()
    print()
    print(f"大模型：{config.LLM_MODEL} ({config.LLM_API_BASE})")
    print(f"轮询间隔：{config.POLL_INTERVAL}s")
    print(f"监听设备：{len(speakers)} 台")
    for s in speakers:
        print(f"  - {s.name} ({s.device_id[:8]}...)")
    print()
    print("已启动，对小爱音箱说话即可对话...")
    print("按 Ctrl+C 退出")
    print("-" * 50)

    # 每台设备独立去重：同样的问话在两台音箱上先后说，不应互相吞掉
    tasks = []
    skipped = 0
    for s in speakers:
        processed: set = set()
        state = {"last_query": "", "last_reply_time": 0.0}
        result = await s.get_latest_ask()
        if result:
            q, ts = result
            processed.add(ts)
            state["last_query"] = q
            state["last_reply_time"] = time()
            skipped += 1
        tasks.append(_poll_loop(s, processed, state))
    logger.info(f"已跳过 {skipped} 条历史对话")

    await asyncio.gather(*tasks, return_exceptions=True)


async def _stop_after_playback(speaker: XiaoAiSpeaker, duration: float):
    """播完音频后播放静音，防止音乐播放器循环"""
    try:
        await asyncio.sleep(duration + 1)
        silence_url = await get_silence_url()
        if silence_url:
            await speaker.play_audio_url(silence_url)
            logger.info(f"[{speaker.name}] 播放完毕，已静音覆盖 ({duration:.1f}s)")
    except Exception as e:
        logger.debug(f"静音覆盖异常：{e}")


# 待执行的静音覆盖任务；新的播放开始前要取消，否则会打断刚放起来的歌
_silence_tasks: dict[str, asyncio.Task] = {}


def _cancel_silence(device_id: str):
    task = _silence_tasks.pop(device_id, None)
    if task and not task.done():
        task.cancel()


def _schedule_silence(speaker: XiaoAiSpeaker, duration: float):
    _cancel_silence(speaker.device_id)
    task = asyncio.create_task(_stop_after_playback(speaker, duration))
    _silence_tasks[speaker.device_id] = task

    def _done(t: asyncio.Task):
        if _silence_tasks.get(speaker.device_id) is t:
            _silence_tasks.pop(speaker.device_id, None)

    task.add_done_callback(_done)


# 长的放前面，避免 "播放音乐" 被 "播放" 先剥掉
MUSIC_PREFIXES = [
    "播放音乐", "播放歌曲", "播放一首歌", "播放一首", "播放首歌", "播放",
    "放几首歌", "放几首", "放一首歌", "放一首", "放首歌", "放个歌", "放音乐", "放歌", "放首",
    "来几首歌", "来几首", "来一首歌", "来一首", "来首歌", "来首",
    "来点音乐", "来点歌曲", "来点歌", "来点",
    "听几首歌", "听几首", "听一首歌", "听一首", "听首歌", "听音乐", "听歌", "听",
    "我想听", "想听",
    "帮我找一下", "帮我搜索一下", "帮我搜索", "帮我搜一下", "搜索一下", "搜索", "搜一下", "帮我搜", "帮我找",
    "找一下", "找", "搜",
    "点一首", "点首歌", "点歌",
    "整几首",
    "放",
]

# “来点X / 放几首X”默认按歌手理解，先去要歌单；歌单里没有再看是不是单曲
PLAYLIST_INTENT_PREFIXES = ("来点", "来几首", "放几首", "听几首", "整几首")

# 这些词出现时说明不是点歌，交给小爱原生处理
NON_MUSIC_HINTS = ["视频", "电影", "电视", "有声书", "评书", "相声", "新闻", "电台", "广播", "播报"]

# 去掉“放/听”之后只剩这些词，说明是在说“放一下”“来点啥”，不是具体歌名
_NO_SONG_WORDS = {"一下", "一个", "什么", "啥", "点什么", "随便", "好听", "好听的", "首", "歌"}


_PLAYLIST_PREFIX_RE = re.compile(
    r"^(?:帮我|给我|麻烦|请)?(?:播放|来几首|放几首|听几首|整几首|来点|放)(.+)$"
)
_POLITE_PREFIX_RE = re.compile(r"^(?:麻烦|请你|请|帮我|给我|能不能|可不可以)\s*")


def _extract_music_keyword(query: str) -> tuple[str, bool] | None:
    """从问话中提取歌曲关键词，返回 (关键词, 是否按歌手找歌单)，不是点歌返回 None"""
    q = query.strip().lstrip("，,。.、 ")
    if any(hint in q for hint in NON_MUSIC_HINTS):
        return None
    q = _POLITE_PREFIX_RE.sub("", q).strip()
    if not q:
        return None
    m = _PLAYLIST_PREFIX_RE.match(q)
    if m:
        rest = m.group(1).strip().lstrip("，,。.、 ")
        if _extract_playlist_artist(rest):
            return rest, True
    for prefix in MUSIC_PREFIXES:
        if q.startswith(prefix):
            tail = q[len(prefix):].strip().lstrip("，,。.、 ")
            if not tail or tail in _NO_SONG_WORDS:
                return None
            keyword = tail.lstrip("一首首的个歌").strip()
            if not keyword or keyword in _NO_SONG_WORDS:
                return None
            return keyword, prefix in PLAYLIST_INTENT_PREFIXES
    return None


def _extract_playlist_artist(keyword: str) -> str | None:
    """“周杰伦的歌/歌曲/歌单”这类诉求返回歌手名"""
    for suffix in ("的歌单", "的歌曲", "的歌", "歌曲", "歌单", "专辑"):
        if keyword.endswith(suffix) and len(keyword) > len(suffix):
            return keyword[:-len(suffix)].strip()
    return None


_VOLUME_RE = re.compile(r"(?:音量|声音)(?:调到|调成|设置为|设为|开到|开到)?\s*(\d{1,3})")


def _parse_control(query: str) -> dict | None:
    """识别播放控制指令"""
    q = query.strip().lstrip("，,。.、 ")
    # 只认短指令，避免“继续讲一下刚才那个”这类聊天被当成播放控制
    if len(q) <= 8:
        if any(k in q for k in ("下一首", "切歌", "换一首", "换首歌", "跳过这首", "跳过")):
            return {"action": "next"}
        if any(k in q for k in ("继续播放", "接着放", "接着播", "继续放", "继续")):
            return {"action": "resume"}
        if any(k in q for k in ("暂停", "停一下", "先停")):
            return {"action": "pause"}
        if any(k in q for k in ("停止播放", "停止音乐", "别放了", "别唱了", "关掉音乐", "停止")):
            return {"action": "stop"}

    m = _VOLUME_RE.search(q)
    if m:
        return {"action": "volume", "value": int(m.group(1))}
    if any(k in q for k in ("声音大", "大声", "音量大")):
        return {"action": "volume", "delta": 10}
    if any(k in q for k in ("声音小", "小声", "音量小")):
        return {"action": "volume", "delta": -10}
    return None


async def _handle_control(speaker: XiaoAiSpeaker, ctrl: dict, entry: dict | None):
    action = ctrl["action"]
    if action == "stop":
        _cancel_segments(speaker.device_id)
        await speaker.stop_playback()
        logger.info(f"[{speaker.name}] 已停止播放")
    elif action == "next":
        if entry:
            entry["skip"] = True
            logger.info(f"[{speaker.name}] 切歌")
        else:
            await speaker.stop_playback()
    elif action == "pause":
        if entry and not entry["paused"]:
            entry["paused"] = True
            entry["paused_at"] = time()
        await speaker.pause_playback()
    elif action == "resume":
        if entry and entry["paused"]:
            entry["paused_total"] += time() - entry["paused_at"]
            entry["paused"] = False
        await speaker.resume_playback()
    elif action == "volume":
        if "value" in ctrl:
            vol = ctrl["value"]
        else:
            status = await speaker.get_status()
            try:
                cur = int(status.get("volume", 50))
            except (TypeError, ValueError):
                cur = 50
            vol = cur + ctrl.get("delta", 0)
        vol = max(1, min(100, vol))
        await speaker.set_volume(vol)


async def _handle_chat(speaker: XiaoAiSpeaker, query: str):
    """走大模型对话并播放语音回复"""
    try:
        reply = await chat(query)
        logger.info(f"[{speaker.name}] AI: {reply}")

        result = await generate_tts(reply)
        if result:
            audio_url, duration = result
            success = await speaker.play_audio_url(audio_url)
            if success:
                _schedule_silence(speaker, duration)
            else:
                logger.warning(f"[{speaker.name}] 音乐通道失败，尝试 TTS")
                await speaker.speak(reply)
        else:
            logger.warning(f"[{speaker.name}] TTS 生成失败，使用内置 TTS")
            await speaker.speak(reply)

    except Exception as e:
        logger.error(f"[{speaker.name}] 大模型调用失败：{e}")
        error_result = await generate_tts("抱歉，我暂时无法回答")
        if error_result:
            error_url, error_duration = error_result
            success = await speaker.play_audio_url(error_url)
            if success:
                _schedule_silence(speaker, error_duration)
        else:
            await speaker.speak("抱歉，我暂时无法回答")


# 分段推流参数：每段约 50 秒（低于 60 秒上限），按音箱真实播放起点接力推送
SEGMENT_SECONDS = 50.0
# 从下发指令到音箱真正出声约 0.7s（实测：推送→拉流 0.37s，拉流→开播 0.32s），
# 提前这么多推送，下一段才正好接在上一段结尾，既不空档也不会让上一段循环
PUSH_LEAD = 0.7
FIRST_FETCH_WAIT = 10.0  # 首段前可能压着小爱的本地回复，等久一点
FETCH_WAIT = 3.0

_seg_playback: dict[str, dict] = {}


def _cancel_segments(device_id: str):
    """停掉正在进行的分段播放"""
    _cancel_silence(device_id)
    entry = _seg_playback.pop(device_id, None)
    if not entry:
        return
    entry["cancelled"] = True
    task = entry.get("task")
    if task and not task.done():
        task.cancel()
    logger.info("已取消分段播放")


def _active_entry(device_id: str) -> dict | None:
    entry = _seg_playback.get(device_id)
    if entry and not entry["cancelled"]:
        return entry
    return None


async def _async_chunks(sync_iter):
    """把同步切片生成器包成异步迭代，边产出边让出事件循环"""
    for item in sync_iter:
        yield item
        await asyncio.sleep(0)


async def _open_track(track: dict):
    """打开一首歌的切片流；非 MP3（FLAC/WAV）先整段下载再转码"""
    if track.get("path"):
        data = Path(track["path"]).read_bytes()
        if not is_mp3(data):
            data = transcode_to_mp3(data)
        return _async_chunks(iter_mp3_chunks(data, SEGMENT_SECONDS)) if data else None

    url = prefer_mp3_url(track["url"])
    head = await fetch_audio_head(url)
    if head is None:
        # 探测都拿不到，别再去整段下载（可能要等 3 分钟），直接交给换源逻辑
        logger.warning(f"音频地址不可用，跳过：{track.get('name')}")
        return None
    if is_mp3(head):
        return stream_mp3_chunks(url, SEGMENT_SECONDS)

    logger.info(f"{track.get('name')} 不是 MP3，下载后转码（约 6-10 秒）")
    raw = await fetch_audio(url, timeout=180)
    if not raw:
        return None
    mp3 = transcode_to_mp3(raw)
    if not mp3:
        return None
    logger.info(f"转码完成 {len(raw) / 1e6:.1f}MB -> {len(mp3) / 1e6:.1f}MB")
    return _async_chunks(iter_mp3_chunks(mp3, SEGMENT_SECONDS))


async def _start_segmented(speaker: XiaoAiSpeaker, tracks: list[dict], title: str,
                           cache_key: str | None = None) -> bool:
    """顺序播放 tracks，每首边下边切片、按音箱真实播放起点接力推送，绕过 60 秒上限"""
    prefix = f"seg_{speaker.device_id[:8]}_{int(time() * 1000)}"
    entry = {
        "cancelled": False,
        "task": None,
        "first_ok": None,
        "first_ready": asyncio.Event(),
        "paused": False,
        "paused_at": 0.0,
        "paused_total": 0.0,
        "skip": False,
    }
    _seg_playback[speaker.device_id] = entry

    async def actual_start(url: str, push_at: float, timeout: float) -> float:
        """音箱真正开始拉流（约等于开播）的时刻，取不到就退回推送时刻"""
        name = url.rsplit("/", 1)[-1]
        t = await wait_fetch(name, timeout=timeout)
        if t is None:
            logger.warning(f"[{speaker.name}] 未检测到拉流（{name}），按推送时间计时")
            return push_at
        return t

    async def wait_target(target: float) -> bool:
        """等到 target（暂停时间自动顺延）；False 表示被取消或切歌"""
        while True:
            if entry["cancelled"] or entry["skip"]:
                return False
            if entry["paused"]:
                await asyncio.sleep(0.1)
                continue
            remain = target + entry["paused_total"] - time()
            if remain <= 0:
                return True
            await asyncio.sleep(min(remain, 0.15))

    def mark_first_failed():
        """首段最终没推出去；只由 runner 调用，换源重试期间不提前报失败"""
        if not entry["first_ready"].is_set():
            entry["first_ok"] = False
            entry["first_ready"].set()

    async def prepare(track: dict, index: int):
        """打开一首歌并启动后台切片，返回 (分段队列, 生产者任务)"""
        try:
            chunks = await _open_track(track)
        except Exception as e:
            logger.warning(f"[{speaker.name}] 打开失败：{track.get('name')} - {e}")
            chunks = None
        if chunks is None:
            logger.warning(f"[{speaker.name}] 无法播放：{track.get('name')}")
            return None

        queue: asyncio.Queue = asyncio.Queue()
        collect = bytearray() if (cache_key and index == 0) else None

        async def producer():
            n = 0
            try:
                async for chunk, dur in chunks:
                    if entry["cancelled"]:
                        break
                    chunk_url = write_chunk(f"{prefix}_{index}", n, chunk)
                    n += 1
                    if collect is not None:
                        collect.extend(chunk)
                    await queue.put((chunk_url, dur))
                else:
                    if collect is not None:
                        song_cache.put(cache_key, track.get("name", ""), track.get("artist", ""), bytes(collect))
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(f"[{speaker.name}] 切片异常：{e}")
            finally:
                await queue.put(None)

        return queue, asyncio.create_task(producer())

    async def play_track(prep, track: dict, index: int, start_at: float | None):
        """推完一首歌的全部分段，返回最后一段的 (开播时刻, 时长)"""
        queue, producer_task = prep
        pushed = 0
        prev_start = 0.0
        prev_dur = 0.0
        try:
            while True:
                # 等第一段时如果用户已经切歌，没必要再等下载/转码
                if entry["cancelled"] or entry["skip"]:
                    return None
                item = await queue.get()
                if item is None:
                    break
                if entry["cancelled"] or entry["skip"]:
                    return None
                chunk_url, dur = item

                if pushed == 0:
                    if start_at is not None and not await wait_target(start_at):
                        return None
                    ok = await speaker.play_audio_url(chunk_url)
                    push_at = time()
                    if not ok:
                        return None
                    if not entry["first_ready"].is_set():
                        entry["first_ok"] = True
                        entry["first_ready"].set()
                    if index == 0:
                        logger.info(f"[{speaker.name}] 首段已推送：{track.get('name')}")
                else:
                    # 上一段播完的瞬间接上这一段，既不空档也不会让它循环
                    if not await wait_target(prev_start + prev_dur - PUSH_LEAD):
                        return None
                    if not await speaker.play_audio_url(chunk_url):
                        # 已经放出去的部分照常播完，按最后一段的时间点交给下一首
                        logger.warning(f"[{speaker.name}] 第 {pushed + 1} 段推送失败，本首中断")
                        return prev_start, prev_dur
                    push_at = time()

                prev_start = await actual_start(
                    chunk_url, push_at,
                    FIRST_FETCH_WAIT if (index == 0 and pushed == 0) else FETCH_WAIT,
                )
                prev_dur = dur
                pushed += 1

            if pushed == 0:
                return None
            logger.info(f"[{speaker.name}] 播放完成：{track.get('name')}")
            return prev_start, prev_dur
        finally:
            if not producer_task.done():
                producer_task.cancel()
                try:
                    await producer_task
                except (asyncio.CancelledError, Exception):
                    pass

    retry_budget = 2

    async def retry_other_source(track: dict) -> dict | None:
        """当前源打不开时，换一个平台再试一次（整场最多两次，避免服务全挂时逐首空等）"""
        nonlocal retry_budget
        url = track.get("url", "")
        if not url or retry_budget <= 0:
            return None
        retry_budget -= 1
        title = track.get("name", "").partition(" - ")[0].strip()
        src = source_of_url(url)
        alt = await switch_source(title, track.get("artist", ""), src)
        if not alt:
            logger.warning(f"[{speaker.name}] 换源失败：{track.get('name')}")
            return None
        logger.info(f"[{speaker.name}] 换源重试：{alt.get('name')} （{src} → {source_of_url(alt['url'])}）")
        return alt

    async def runner():
        last = None
        upcoming = None
        no_prefetch = object()
        prefetched = no_prefetch
        try:
            for index, track in enumerate(tracks):
                if entry["cancelled"]:
                    return
                if prefetched is no_prefetch:
                    prepared = await prepare(track, index)
                else:
                    # 预取已经打开过这首歌，失败也直接换源，不再重复下载
                    prepared = prefetched
                    prefetched = no_prefetch
                if prepared is None and not entry["cancelled"]:
                    alt = await retry_other_source(track)
                    if alt:
                        track = alt
                        tracks[index] = alt
                        prepared = await prepare(alt, 1000 + index)
                if prepared is None:
                    # 这首放不了就跳过；保留 last，下一首仍接在上一首的尾巴后面
                    continue

                start_at = None if last is None else last[0] + last[1] - PUSH_LEAD
                if index + 1 < len(tracks):
                    upcoming = asyncio.create_task(prepare(tracks[index + 1], index + 1))

                res = await play_track(prepared, track, index, start_at)

                if entry["cancelled"]:
                    return
                # 一段都没推出去，多半是这个源的文件拿不到，换源重试
                if res is None and not entry["skip"]:
                    alt = await retry_other_source(track)
                    if alt:
                        track = alt
                        tracks[index] = alt
                        retry_prep = await prepare(alt, 1000 + index)
                        if retry_prep:
                            res = await play_track(retry_prep, alt, index, start_at)

                if entry["cancelled"]:
                    return
                if entry["skip"]:
                    logger.info(f"[{speaker.name}] 切歌：{track.get('name')}")
                    entry["skip"] = False
                    last = None   # 下一首的首段已经在队列里，直接接上
                else:
                    last = res

                if upcoming is not None:
                    prefetched = await upcoming
                    upcoming = None

            if last is None:
                mark_first_failed()
                await speaker.stop_playback()
                return

            # 最后一段播完的瞬间顶静音，避免音箱把末段从头循环
            if not await wait_target(last[0] + last[1] - PUSH_LEAD):
                if entry["skip"] and not entry["cancelled"]:
                    await speaker.stop_playback()
                return
            silence_url = await get_silence_url()
            if silence_url:
                await speaker.play_audio_url(silence_url)
            logger.info(f"[{speaker.name}] 分段播放结束：{title}（{len(tracks)} 首）")
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error(f"[{speaker.name}] 分段播放异常：{e}")
            mark_first_failed()
        finally:
            if upcoming is not None and not upcoming.done():
                upcoming.cancel()
            cleanup_chunks(prefix)
            if _seg_playback.get(speaker.device_id) is entry:
                _seg_playback.pop(speaker.device_id, None)

    entry["task"] = asyncio.create_task(runner())
    try:
        # 首段正常 3~5 秒；失败后还要留出换源 + 转码的时间
        await asyncio.wait_for(entry["first_ready"].wait(), timeout=40)
    except asyncio.TimeoutError:
        logger.warning(f"[{speaker.name}] 首段推送超时")
        _cancel_segments(speaker.device_id)
        return False

    return bool(entry["first_ok"])


async def _resolve_tracks(keyword: str, artist_intent: bool = False) -> tuple[list[dict], str, str | None]:
    """把点歌关键词变成待播列表；命中缓存直接播本地文件"""
    # 歌单诉求别命中单曲缓存，否则“来点周杰伦”会只剩上次缓存的那一首
    cached = None if artist_intent else song_cache.lookup(keyword)
    if cached:
        name = f"{cached['name']} - {cached['artist']}" if cached["artist"] else cached["name"]
        logger.info(f"缓存命中：{name}，跳过搜索")
        return [{"path": cached["path"], "name": cached["name"], "artist": cached["artist"]}], name, None

    artist = _extract_playlist_artist(keyword)
    if artist is None and artist_intent:
        artist = keyword.strip()
    if artist:
        pairs = await search_artist_tracks(artist)
        if pairs:
            logger.info(f"歌单：{artist} 共 {len(pairs)} 首")
            tracks = [{"url": url, "name": name, "artist": artist} for url, name in pairs]
            return tracks, f"{artist} 的歌", None

    hit = await search_and_play(keyword)
    if not hit:
        return [], keyword, None
    url, name = hit
    title, _, song_artist = name.partition(" - ")
    return [{"url": url, "name": title, "artist": song_artist}], name, keyword


async def _poll_loop(speaker: XiaoAiSpeaker, processed: set, state: dict):
    processing = False
    COOLDOWN = 60

    while True:
        try:
            result = await speaker.get_latest_ask()

            if result and not processing:
                query, timestamp_ms = result

                is_dup = (
                    timestamp_ms in processed
                    or (query == state["last_query"]
                        and time() - state["last_reply_time"] < COOLDOWN)
                )

                # 检查是否是点歌指令（播放/放/来一首/听/搜索…）
                music = _extract_music_keyword(query)

                operation_keywords = [
                    # 音乐/音频控制
                    "播放", "放歌", "听音乐", "切歌", "关歌", "下一首", "上一首", "换歌", "跳过",
                    "暂停", "停止", "音量", "声音", "大声", "小声", "调高", "调低",
                    # 设备开关
                    "打开", "关闭", "关掉", "开启",
                    # 环境调节
                    "设置", "调节", "变大", "变小", "亮一点", "暗一点",
                    "温度", "湿度", "亮度",
                    # 天气
                    "天气", "下雨", "下雪", "晴天", "阴天",
                    # 清洁设备
                    "扫地", "清扫", "回充",
                    # 空调/暖通
                    "空调", "暖气", "制冷", "制热", "风扇", "吹风",
                    # 电视/媒体
                    "电视", "电影", "视频",
                    # 灯光
                    "灯", "开灯", "关灯",
                    # 厨房电器
                    "电饭煲", "微波炉", "烤箱", "洗碗机",
                    # 其他家电
                    "洗衣机", "冰箱", "窗帘", "门窗", "热水器", "浴霸"
                ]
                is_operation = any(kw in query for kw in operation_keywords)

                ctrl = _parse_control(query)
                entry = _active_entry(speaker.device_id)

                if is_dup:
                    logger.debug(f"[{speaker.name}] 跳过重复：ts={timestamp_ms}")
                elif ctrl and (entry or ctrl["action"] == "volume"):
                    processed.add(timestamp_ms)
                    state["last_query"] = query
                    state["last_reply_time"] = time()
                    processing = True
                    logger.info(f"[{speaker.name}] 播放控制：{query}")
                    try:
                        await _handle_control(speaker, ctrl, entry)
                    finally:
                        processing = False
                elif music:
                    music_keyword, artist_intent = music
                    processing = True
                    logger.info(f"[{speaker.name}] 点歌：{music_keyword}")

                    _cancel_segments(speaker.device_id)
                    # 小爱原生服务通常已经抢先开播，先掐断再推我们的流
                    await speaker.stop_playback()

                    played = False
                    try:
                        tracks, title, cache_key = await _resolve_tracks(music_keyword, artist_intent)
                        if tracks:
                            logger.info(
                                f"[{speaker.name}] 找到歌曲：{title}"
                                + (f"（{len(tracks)} 首）" if len(tracks) > 1 else "")
                            )
                            played = await _start_segmented(speaker, tracks, title, cache_key)
                            if not played and tracks[0].get("url"):
                                played = await speaker.play_audio_url(tracks[0]["url"])
                            if played:
                                logger.info(f"[{speaker.name}] 开始播放：{title}")
                            else:
                                logger.warning(f"[{speaker.name}] 推流失败，转 AI 处理")
                        else:
                            logger.info(f"[{speaker.name}] 未找到歌曲，转 AI 处理")
                    except Exception as e:
                        logger.error(f"[{speaker.name}] 搜索音乐异常：{e}")
                    finally:
                        processed.add(timestamp_ms)
                        state["last_query"] = query
                        state["last_reply_time"] = time()
                        processing = False

                    if not played:
                        processing = True
                        try:
                            logger.info(f"[{speaker.name}] 用户：{query}")
                            await _handle_chat(speaker, query)
                        finally:
                            processing = False
                elif is_operation:
                    _cancel_segments(speaker.device_id)
                    logger.info(f"[{speaker.name}] 跳过操作指令，由小爱处理：{query}")
                    processed.add(timestamp_ms)
                    state["last_query"] = query
                    state["last_reply_time"] = time()
                else:
                    _cancel_segments(speaker.device_id)
                    processed.add(timestamp_ms)
                    state["last_query"] = query
                    state["last_reply_time"] = time()
                    processing = True
                    logger.info(f"[{speaker.name}] 用户：{query}")

                    try:
                        await _handle_chat(speaker, query)
                    finally:
                        processing = False

            await asyncio.sleep(config.POLL_INTERVAL)

        except KeyboardInterrupt:
            break
        except Exception as e:
            logger.error(f"[{speaker.name}] 轮询异常：{e}")
            await asyncio.sleep(config.POLL_INTERVAL)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
