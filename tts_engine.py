import asyncio
import logging
import os
import socket
import tempfile
import threading
import time
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import aiohttp
import edge_tts
from mutagen.mp3 import MP3

logger = logging.getLogger(__name__)

TTS_DIR = Path(tempfile.gettempdir()) / "xiaoai_tts"


def ensure_tts_dir() -> Path:
    """%TEMP% 下的目录随时可能被存储感知/磁盘清理删掉

    只在模块导入时建一次是不够的：进程长期运行时目录一旦消失，
    之后所有 write_bytes 都抛 FileNotFoundError，表现为切片失败、
    首段推送超时、TTS 全部生成失败。所以每次写入前都补齐。
    """
    TTS_DIR.mkdir(parents=True, exist_ok=True)
    return TTS_DIR


ensure_tts_dir()

TTS_VOICE = "zh-CN-XiaoxiaoNeural"

_server_started = False
_server_port = 0
_server_ip = ""

# 音箱实际来拉流的时间，用来对齐真实播放起点
_fetch_times: dict[str, float] = {}


async def wait_fetch(name: str, timeout: float = 3.0, tick: float = 0.05) -> float | None:
    """等音箱请求指定文件，返回请求时刻；超时返回 None"""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        t = _fetch_times.get(name)
        if t is not None:
            return t
        await asyncio.sleep(tick)
    return None


def _get_local_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"
    finally:
        s.close()


class _QuietHandler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(TTS_DIR), **kwargs)

    def do_GET(self):
        started = time.time()
        name = self.path.split("?", 1)[0].lstrip("/")
        _fetch_times[name] = started
        rng = self.headers.get("Range", "-")
        ua = (self.headers.get("User-Agent") or "")[:30]
        super().do_GET()
        logger.info(
            f"HTTP {self.path} Range={rng} UA={ua} 用时{time.time() - started:.2f}s"
        )

    def log_message(self, format, *args):
        pass


def _start_server():
    global _server_started, _server_port, _server_ip
    if _server_started:
        return

    server = ThreadingHTTPServer(("0.0.0.0", 0), _QuietHandler)
    _server_port = server.server_address[1]
    _server_ip = _get_local_ip()

    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    _server_started = True
    logger.info(f"TTS 文件服务已启动: http://{_server_ip}:{_server_port}")


def ensure_server() -> tuple[str, int]:
    """确保本地文件服务已启动，返回 (ip, port)"""
    _start_server()
    return _server_ip, _server_port


def public_url(filename: str) -> str:
    """把 TTS_DIR 下的文件名转成可访问 URL"""
    ip, port = ensure_server()
    return f"http://{ip}:{port}/{filename}"


async def generate_tts(text: str, retries: int = 3) -> tuple[str, float] | None:
    """用 edge-tts 生成 MP3 文件，返回 (URL, 时长秒数)"""
    _start_server()
    ensure_tts_dir()

    filename = f"tts_{hash(text) & 0xFFFFFFFF}.mp3"
    filepath = TTS_DIR / filename
    duration_file = filepath.with_suffix(".duration")

    if filepath.exists() and duration_file.exists():
        try:
            duration = float(duration_file.read_text())
            url = f"http://{_server_ip}:{_server_port}/{filename}"
            return url, duration
        except Exception:
            pass

    if not filepath.exists():
        for attempt in range(retries):
            try:
                communicate = edge_tts.Communicate(text, TTS_VOICE)
                await communicate.save(str(filepath))
                logger.info(f"TTS 音频已生成: {filepath.name} ({filepath.stat().st_size} bytes)")
                break
            except Exception as e:
                logger.warning(f"edge-tts 生成失败 (尝试 {attempt+1}/{retries}): {e}")
                if attempt < retries - 1:
                    await asyncio.sleep(1)
                else:
                    logger.error(f"edge-tts 生成最终失败")
                    return None

    try:
        audio = MP3(filepath)
        duration = audio.info.length
        duration_file.write_text(f"{duration:.2f}")
        logger.info(f"MP3 时长: {duration:.1f}s")
    except Exception as e:
        logger.warning(f"读取 MP3 时长失败: {e}")
        duration = 0

    url = f"http://{_server_ip}:{_server_port}/{filename}"
    return url, duration


_silence_url: str | None = None


async def get_silence_url() -> str | None:
    """生成一段静音 MP3 的 URL，用于覆盖循环播放"""
    global _silence_url
    if _silence_url:
        return _silence_url

    silence_path = TTS_DIR / "silence.mp3"
    if not silence_path.exists():
        ensure_tts_dir()
        # 生成有效的静音 MP3 (MPEG1 Layer3, 48kbps, 24kHz, mono)
        # 每帧 144 字节，约 72ms，生成约 2 秒静音
        frame_header = bytes([0xFF, 0xFB, 0x60, 0xC4])
        frame_data = frame_header + b'\x00' * 140
        try:
            with open(silence_path, 'wb') as f:
                for _ in range(28):
                    f.write(frame_data)
            logger.info(f"静音文件已生成: {silence_path.stat().st_size} bytes")
        except Exception as e:
            logger.error(f"静音文件生成失败: {e}")
            return None

    _start_server()
    _silence_url = f"http://{_server_ip}:{_server_port}/silence.mp3"
    return _silence_url
