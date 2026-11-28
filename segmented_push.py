"""MP3 帧级切片 + 分段推流到小爱音箱

不需要 ffmpeg：MPEG 音频帧彼此独立，只要在帧边界切开，每段都能独立解码。
"""
import asyncio
import logging
from pathlib import Path

import aiohttp

from tts_engine import TTS_DIR, ensure_server, ensure_tts_dir, public_url

logger = logging.getLogger(__name__)

# MPEG1 Layer3 / MPEG2(Layer3) 码率表 (kbps)
_BITRATES_V1_L3 = [0, 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320]
_BITRATES_V2_L3 = [0, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160]
_SAMPLE_RATES = {3: [44100, 48000, 32000], 2: [22050, 24000, 16000], 0: [11025, 12000, 8000]}


def _id3_size(data: bytes) -> int:
    """ID3v2 头部长度（含标签本身），无标签返回 0"""
    if len(data) < 10 or data[:3] != b"ID3":
        return 0
    size = (data[6] & 0x7F) << 21 | (data[7] & 0x7F) << 14 | (data[8] & 0x7F) << 7 | (data[9] & 0x7F)
    footer = 10 if data[5] & 0x10 else 0
    return 10 + size + footer


def _frame_info(data: bytes, pos: int):
    """解析 pos 处的帧头，返回 (帧长, 时长秒) 或 None"""
    if pos + 4 > len(data) or data[pos] != 0xFF or (data[pos + 1] & 0xE0) != 0xE0:
        return None
    b1, b2 = data[pos + 1], data[pos + 2]
    version = (b1 >> 3) & 0x03
    layer = (b1 >> 1) & 0x03
    if version == 1 or layer != 1:  # 保留版本 / 只处理 Layer III
        return None
    table = _BITRATES_V1_L3 if version == 3 else _BITRATES_V2_L3
    bitrate_idx = (b2 >> 4) & 0x0F
    rate_idx = (b2 >> 2) & 0x03
    if bitrate_idx in (0, 15) or rate_idx == 3:
        return None
    bitrate = table[bitrate_idx] * 1000
    sample_rate = _SAMPLE_RATES[version][rate_idx]
    padding = (b2 >> 1) & 0x01
    if version == 3:
        return 144 * bitrate // sample_rate + padding, 1152 / sample_rate
    return 72 * bitrate // sample_rate + padding, 576 / sample_rate


def mp3_duration_head(data: bytes) -> float | None:
    """从文件头读 Xing/Info 帧数算总时长，没有索引头返回 None"""
    pos = _id3_size(data)
    info = _frame_info(data, pos)
    if info is None:
        return None
    frame_len, frame_dur = info
    hdr = data[pos + 4:pos + 4 + frame_len]
    marker = hdr.find(b"Xing")
    if marker < 0:
        marker = hdr.find(b"Info")
    if marker < 0 or len(hdr) < marker + 12:
        return None
    flags = int.from_bytes(hdr[marker + 4:marker + 8], "big")
    if not flags & 1:
        return None
    frames = int.from_bytes(hdr[marker + 8:marker + 12], "big")
    return frames * frame_dur if frames else None


def mp3_bitrate_head(data: bytes) -> int | None:
    """第一帧的码率（bps），用来按文件大小估算时长"""
    pos = _id3_size(data)
    info = _frame_info(data, pos)
    if info is None:
        return None
    frame_len, frame_dur = info
    return int(frame_len * 8 / frame_dur)


def is_mp3(data: bytes) -> bool:
    if data[:3] == b"ID3":
        return True
    return _frame_info(data, 0) is not None


def split_mp3(data: bytes, chunk_seconds: float = 50.0) -> list[bytes]:
    """按帧边界把 MP3 切成若干段，每段约 chunk_seconds 秒"""
    pos = _id3_size(data)
    chunks: list[bytes] = []

    # 首个帧可能是 Xing/Info 头（VBR 索引），不含音频且会让播放器误判总时长，跳过
    first = _frame_info(data, pos)
    if first and (b"Xing" in data[pos + 4:pos + 4 + 40] or b"Info" in data[pos + 4:pos + 4 + 40]):
        pos += first[0]

    start = pos
    elapsed = 0.0

    while pos + 4 <= len(data):
        info = _frame_info(data, pos)
        if info is None:
            pos += 1
            continue
        frame_len, frame_dur = info
        if pos + frame_len > len(data):
            break
        elapsed += frame_dur
        pos += frame_len
        if elapsed >= chunk_seconds:
            chunks.append(data[start:pos])
            start = pos
            elapsed = 0.0

    if start < len(data):
        chunks.append(data[start:])
    return [c for c in chunks if len(c) > 1024]


def iter_mp3_chunks(data: bytes, chunk_seconds: float = 50.0):
    """对已完整拿到手的 MP3 按帧切片，yield (数据, 该段时长)"""
    pos = _id3_size(data)
    first = _frame_info(data, pos)
    if first and (b"Xing" in data[pos + 4:pos + 44] or b"Info" in data[pos + 4:pos + 44]):
        pos += first[0]

    start = pos
    elapsed = 0.0
    while pos + 4 <= len(data):
        info = _frame_info(data, pos)
        if info is None:
            pos += 1
            continue
        frame_len, frame_dur = info
        if pos + frame_len > len(data):
            break
        elapsed += frame_dur
        pos += frame_len
        if elapsed >= chunk_seconds:
            yield data[start:pos], elapsed
            start = pos
            elapsed = 0.0

    if len(data) - start > 1024:
        yield data[start:], elapsed


def transcode_to_mp3(data: bytes, bitrate: int = 128) -> bytes | None:
    """FLAC/WAV 等整段解码后重新编码成 MP3，失败返回 None"""
    try:
        import io

        import lameenc
        import soundfile as sf
    except ImportError:
        logger.warning("缺少 soundfile/lameenc，无法转码 FLAC")
        return None

    try:
        with sf.SoundFile(io.BytesIO(data)) as f:
            if f.frames / f.samplerate > 900:
                logger.warning("音频超过 15 分钟，跳过转码")
                return None
            rate, channels = f.samplerate, f.channels
            pcm = f.read(dtype="int16")
        if not len(pcm):
            return None
        enc = lameenc.Encoder()
        enc.set_bit_rate(bitrate)
        enc.set_in_sample_rate(rate)
        enc.set_channels(channels)
        enc.set_quality(5)
        return enc.encode(pcm.tobytes()) + enc.flush()
    except Exception as e:
        logger.warning(f"转码失败: {e}")
        return None


async def fetch_audio(url: str, timeout: float = 30.0) -> bytes | None:
    """完整下载音频"""
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=timeout)) as resp:
                if resp.status not in (200, 206):
                    logger.warning(f"下载音频失败: {resp.status}")
                    return None
                return await resp.read()
    except Exception as e:
        logger.warning(f"下载音频异常: {e}")
        return None


async def fetch_audio_head(url: str, size: int = 4096, timeout: float = 10.0) -> bytes | None:
    """只取开头一小段，用于判断是不是 MP3"""
    try:
        async with aiohttp.ClientSession() as session:
            headers = {"Range": f"bytes=0-{size - 1}"}
            async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=timeout)) as resp:
                if resp.status not in (200, 206):
                    logger.warning(f"探测音频失败: {resp.status}")
                    return None
                return await resp.content.read(size)
    except Exception as e:
        logger.warning(f"探测音频异常: {e}")
        return None


async def stream_mp3_chunks(url: str, chunk_seconds: float = 50.0, timeout: float = 120.0):
    """边下载边按帧切片，yield (数据, 该段实际时长秒数)；首段不必等整个文件下载完"""
    async with aiohttp.ClientSession() as session:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=timeout, sock_read=30)) as resp:
            if resp.status not in (200, 206):
                logger.warning(f"下载音频失败: {resp.status}")
                return

            buf = bytearray()
            pos = 0
            chunk_start = 0
            elapsed = 0.0
            started = False

            async for block in resp.content.iter_chunked(64 * 1024):
                buf.extend(block)

                if not started:
                    if len(buf) < 10:
                        continue
                    id3 = _id3_size(bytes(buf))
                    if id3 and len(buf) < id3 + 4:
                        continue
                    pos = id3
                    info = _frame_info(buf, pos)
                    if info is not None and (b"Xing" in buf[pos + 4:pos + 44] or b"Info" in buf[pos + 4:pos + 44]):
                        if len(buf) < pos + info[0]:
                            continue
                        pos += info[0]
                    chunk_start = pos
                    started = True

                while pos + 4 <= len(buf):
                    info = _frame_info(buf, pos)
                    if info is None:
                        pos += 1
                        continue
                    frame_len, frame_dur = info
                    if pos + frame_len > len(buf):
                        break
                    elapsed += frame_dur
                    pos += frame_len
                    if elapsed >= chunk_seconds:
                        yield bytes(buf[chunk_start:pos]), elapsed
                        chunk_start = pos
                        elapsed = 0.0

                # 丢弃已产出的数据，避免整个文件堆在内存里
                if chunk_start > 1024 * 1024:
                    del buf[:chunk_start]
                    pos -= chunk_start
                    chunk_start = 0

            if len(buf) - chunk_start > 1024:
                yield bytes(buf[chunk_start:]), elapsed


def write_chunk(prefix: str, index: int, chunk: bytes) -> Path:
    """写入一段音频并返回可访问 URL"""
    ensure_tts_dir()
    ensure_server()
    path = Path(TTS_DIR) / f"{prefix}_{index:03d}.mp3"
    path.write_bytes(chunk)
    return public_url(path.name)


def publish_chunks(chunks: list[bytes], prefix: str) -> list[str]:
    """把分段写入本地 HTTP 目录，返回可访问 URL 列表"""
    ensure_tts_dir()
    ensure_server()
    urls = []
    for i, chunk in enumerate(chunks):
        path = Path(TTS_DIR) / f"{prefix}_{i:03d}.mp3"
        path.write_bytes(chunk)
        urls.append(public_url(path.name))
    return urls


def cleanup_chunks(prefix: str):
    for path in Path(TTS_DIR).glob(f"{prefix}_*.mp3"):
        try:
            path.unlink()
        except OSError:
            pass


async def play_segmented(speaker, urls: list[str], chunk_seconds: float,
                         lead_seconds: float = 3.0, is_cancelled=None) -> bool:
    """依次把分段推给音箱，提前 lead_seconds 推下一段实现连续播放"""
    if not urls:
        return False

    for i, url in enumerate(urls):
        if is_cancelled and is_cancelled():
            logger.info(f"[{speaker.name}] 分段播放被取消")
            return False
        ok = await speaker.play_audio_url(url)
        if not ok:
            logger.warning(f"[{speaker.name}] 第 {i+1}/{len(urls)} 段推送失败")
            return False
        if i == len(urls) - 1:
            break
        await asyncio.sleep(max(1.0, chunk_seconds - lead_seconds))
    return True
