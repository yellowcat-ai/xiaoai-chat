"""局域网播报接口：POST /notify 让指定音箱说一句话

鉴权用 token，来源优先级：环境变量 NOTIFY_TOKEN → secrets-manager 里的 xiaoai_notify_token。
手机快捷指令、自动化脚本、Cloudflare Tunnel 暴露后都走同一个接口。
"""
import hmac
import logging
import os
import subprocess
from pathlib import Path

from aiohttp import web

logger = logging.getLogger(__name__)

SECRETS_CLI = Path.home() / ".secrets-manager" / "secrets.js"
TOKEN_NAME = "xiaoai_notify_token"
MAX_TEXT = 300
MAX_AUDIO_URL = 2000

_runner: web.AppRunner | None = None


def _load_token() -> str:
    token = os.getenv("NOTIFY_TOKEN", "").strip()
    if token:
        return token
    try:
        out = subprocess.run(
            ["node", str(SECRETS_CLI), "get", TOKEN_NAME],
            capture_output=True, text=True, timeout=10, encoding="utf-8",
        )
    except Exception as e:
        logger.warning(f"读取播报 token 失败：{e}")
        return ""
    token = (out.stdout or "").strip()
    if not token:
        logger.warning(f"secrets-manager 里没有 {TOKEN_NAME}，播报接口不会启动")
    return token


def _match_device(speakers, key: str):
    """按设备名（子串）或 device_id 找音箱；不指定时优先客厅"""
    key = (key or "").strip()
    if not key:
        for s in speakers:
            if "客厅" in (s.name or ""):
                return s
        return speakers[0] if speakers else None
    low = key.lower()
    for s in speakers:
        if low == (s.device_id or "").lower():
            return s
    for s in speakers:
        if low in (s.name or "").lower():
            return s
    return None


def create_app(speakers, on_text, on_audio):
    token = _load_token()
    if not token:
        return None

    def authorized(request: web.Request) -> bool:
        supplied = request.headers.get("X-Notify-Token") or request.query.get("token", "")
        return hmac.compare_digest(supplied or "", token)

    async def health(request: web.Request):
        if not authorized(request):
            return web.json_response({"ok": False, "error": "unauthorized"}, status=401)
        return web.json_response({
            "ok": True,
            "devices": [{"name": s.name, "id": s.device_id} for s in speakers],
        })

    async def notify(request: web.Request):
        if not authorized(request):
            return web.json_response({"ok": False, "error": "unauthorized"}, status=401)
        try:
            data = await request.json()
        except Exception:
            return web.json_response({"ok": False, "error": "invalid json"}, status=400)
        if not isinstance(data, dict):
            return web.json_response({"ok": False, "error": "invalid json"}, status=400)

        text = str(data.get("text") or "").strip()[:MAX_TEXT]
        audio_url = str(data.get("audio_url") or "").strip()[:MAX_AUDIO_URL]
        if not text and not audio_url:
            return web.json_response(
                {"ok": False, "error": "text or audio_url required"}, status=400)
        if audio_url and not audio_url.startswith(("http://", "https://")):
            return web.json_response({"ok": False, "error": "audio_url must be http(s)"}, status=400)

        speaker = _match_device(speakers, str(data.get("device") or ""))
        if speaker is None:
            return web.json_response(
                {"ok": False, "error": "device not found",
                 "devices": [s.name for s in speakers]}, status=404)

        interrupt = data.get("interrupt", True) is not False
        try:
            if text:
                ok = await on_text(speaker, text, interrupt)
                # 手机快捷指令里只回 ok 就行，日志里保留完整内容
                logger.info(f"[{speaker.name}] 播报：{text[:60]}")
            else:
                ok = await on_audio(speaker, audio_url, interrupt)
                logger.info(f"[{speaker.name}] 播报音频：{audio_url[:80]}")
        except Exception as e:
            logger.error(f"播报失败：{e}")
            return web.json_response({"ok": False, "error": str(e)}, status=500)

        return web.json_response({"ok": bool(ok), "device": speaker.name})

    app = web.Application()
    app.router.add_get("/healthz", health)
    app.router.add_post("/notify", notify)
    return app


async def start(speakers, on_text, on_audio, port: int, host: str = "0.0.0.0") -> bool:
    """启动播报服务；没有 token 时返回 False，不影响主程序"""
    global _runner
    app = create_app(speakers, on_text, on_audio)
    if app is None:
        return False
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, host, port)
    await site.start()
    _runner = runner
    logger.info(f"播报接口已启动: http://{host}:{port}/notify")
    return True


async def stop():
    global _runner
    if _runner is not None:
        await _runner.cleanup()
        _runner = None
