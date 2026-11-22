import asyncio
import logging
import os
from json import loads
from time import time

from miservice import MiAccount, MiNAService
from miservice.miaccount import UA_MINA

logger = logging.getLogger(__name__)

OTP_FILE = os.path.join(os.path.dirname(__file__), ".otp_code")

CONVERSATION_API = (
    "https://userprofile.mina.mi.com/device_profile/v2/conversation"
    "?source=dialogu&hardware={hardware}&timestamp={timestamp}&limit=2"
)


async def file_otp_callback(otp_method: str) -> str:
    print(f"\n!!! 需要验证码，请把验证码写入文件: {OTP_FILE}")
    print(f"!!! 例如: echo 123456 > {OTP_FILE}\n")
    while True:
        if os.path.exists(OTP_FILE):
            with open(OTP_FILE, "r") as f:
                code = f.read().strip()
            if code:
                os.remove(OTP_FILE)
                print(f"收到验证码: {code}")
                return code
        await asyncio.sleep(1)


_login_lock = asyncio.Lock()
_last_relogin = 0.0

# 小米接口返回 code=999 表示限流/繁忙，全局退避，避免越打越限
_busy_until = 0.0
_busy_count = 0
_last_busy_warn = 0.0


def _note_busy():
    global _busy_until, _busy_count, _last_busy_warn
    _busy_count += 1
    delay = min(60.0, 5.0 * (2 ** min(_busy_count - 1, 4)))
    _busy_until = max(_busy_until, time() + delay)
    if time() - _last_busy_warn > 60:
        _last_busy_warn = time()
        logger.warning(f"小米接口繁忙(code 999)，全局暂停轮询 {delay:.0f}s")


def _note_ok():
    global _busy_count
    _busy_count = 0


class XiaoAiSpeaker:
    def __init__(self, account: MiAccount, service: MiNAService,
                 device_id: str, hardware: str, name: str = ""):
        self.account = account
        self.service = service
        self.device_id = device_id
        self.hardware = hardware
        self.name = name
        self._last_timestamp_ms: int = 0

    async def _relogin(self) -> bool:
        """token 过期后重新登录（多设备共用账号，60 秒内只刷一次）"""
        global _last_relogin
        async with _login_lock:
            if time() - _last_relogin < 60:
                return True
            logger.warning("serviceToken 可能已过期，重新登录小米账号")
            try:
                ok = await self.account.login("micoapi")
            except Exception as e:
                logger.error(f"重新登录异常: {e}")
                return False
            _last_relogin = time()
            if ok and self.account.token_store and self.account.token:
                await self.account.token_store.save_token(self.account.token)
            logger.info(f"重新登录{'成功' if ok else '失败'}")
            return ok

    async def get_latest_ask(self) -> tuple[str, int] | None:
        """通过云端对话历史 API 获取最新一条用户提问"""
        if time() < _busy_until:
            await asyncio.sleep(_busy_until - time())

        url = CONVERSATION_API.format(
            hardware=self.hardware,
            timestamp=int(time() * 1000),
        )
        headers = {"User-Agent": UA_MINA}

        for attempt in range(3):
            cookies = {
                "userId": self.account.token["userId"],
                "serviceToken": self.account.token["micoapi"][1],
                "deviceId": self.device_id,
            }
            try:
                async def do_request():
                    async with self.account.request(url, cookies=cookies, headers=headers) as r:
                        if r.status in (401, 403):
                            return {"__auth__": r.status}
                        if r.status != 200:
                            body = (await r.text())[:200]
                            logger.warning(f"API 返回状态: {r.status} {body}")
                            return None
                        return await r.json(content_type=None)

                resp = await asyncio.wait_for(do_request(), timeout=10.0)
                if resp is None:
                    return None
                if "__auth__" in resp:
                    await self._relogin()
                    continue

                if resp.get("code") != 0:
                    if resp.get("code") == 999:
                        _note_busy()
                    else:
                        logger.warning(f"API code: {resp.get('code')} {str(resp.get('message', ''))[:100]}")
                    return None
                _note_ok()

                data_str = resp.get("data", "")
                if not data_str:
                    return None

                data = loads(data_str) if isinstance(data_str, str) else data_str
                records = data.get("records", [])
                if not records:
                    logger.debug("云端对话记录为空")
                    return None

                latest = records[0]
                ts = latest.get("time", 0)
                query = latest.get("query", "").strip()
                if not query:
                    return None

                logger.debug(f"云端检测到提问: {query} (ts={ts})")
                return query, ts

            except asyncio.TimeoutError:
                if attempt < 2:
                    logger.debug(f"API 超时 (尝试 {attempt+1}/3)")
                    await asyncio.sleep(2)
                else:
                    logger.warning(f"API 超时 (已重试3次)")
                    return None
            except Exception as e:
                if attempt < 2:
                    logger.debug(f"获取对话失败 (尝试 {attempt+1}/3): {e}")
                    await asyncio.sleep(2)
                else:
                    logger.warning(f"获取对话失败 (已重试3次): {e}")
                    return None
        return None

    def is_new(self, timestamp_ms: int) -> bool:
        if timestamp_ms > self._last_timestamp_ms:
            self._last_timestamp_ms = timestamp_ms
            return True
        return False

    async def speak(self, text: str):
        """通过 TTS 让音箱说话（仅在对话模式下有效）"""
        try:
            await self.service.text_to_speech(self.device_id, text)
            logger.info(f"[{self.name}] TTS: {text[:60]}...")
        except Exception as e:
            logger.error(f"[{self.name}] TTS 失败: {e}")

    async def play_audio_url(self, url: str, audio_id: str | None = None):
        """通过音乐通道播放音频 URL；网络抖动时重试，避免整首歌中断"""
        for attempt in range(3):
            try:
                kwargs = {"audio_id": audio_id} if audio_id else {}
                result = await self.service.play_by_music_url(self.device_id, url, **kwargs)
                logger.info(f"[{self.name}] 音乐通道播放: {url[:80]}... (result={result})")
                return result
            except Exception as e:
                if attempt == 2:
                    logger.error(f"[{self.name}] 音乐通道播放失败: {e}")
                    return False
                logger.warning(f"[{self.name}] 音乐通道异常，重试({attempt + 1}/2): {e}")
                await asyncio.sleep(0.4 * (attempt + 1))
        return False

    async def stop_playback(self):
        """停止当前播放"""
        try:
            result = await self.service.player_stop(self.device_id)
            logger.debug(f"[{self.name}] 停止播放 (result={result})")
            return result
        except Exception as e:
            logger.error(f"[{self.name}] 停止播放失败: {e}")
            return False

    async def pause_playback(self):
        try:
            result = await self.service.player_pause(self.device_id)
            logger.info(f"[{self.name}] 暂停 (result={result})")
            return result
        except Exception as e:
            logger.error(f"[{self.name}] 暂停失败: {e}")
            return False

    async def resume_playback(self):
        try:
            result = await self.service.player_play(self.device_id)
            logger.info(f"[{self.name}] 继续 (result={result})")
            return result
        except Exception as e:
            logger.error(f"[{self.name}] 继续失败: {e}")
            return False

    async def set_volume(self, volume: int):
        try:
            result = await self.service.player_set_volume(self.device_id, volume)
            logger.info(f"[{self.name}] 音量 {volume} (result={result})")
            return result
        except Exception as e:
            logger.error(f"[{self.name}] 设置音量失败: {e}")
            return False

    async def get_status(self) -> dict:
        try:
            return await self.service.player_get_status(self.device_id) or {}
        except Exception as e:
            logger.debug(f"[{self.name}] 获取播放状态失败: {e}")
            return {}


async def create_speakers() -> list[XiaoAiSpeaker]:
    import config

    account = MiAccount(None, config.MI_USER, config.MI_PASS, otp_callback=file_otp_callback)

    if account.token_store:
        saved_token = await account.token_store.load_token()
        if saved_token:
            account.token = saved_token
            print("已加载保存的登录状态")

    ok = await account.login("micoapi")
    if not ok:
        raise RuntimeError(f"小米账号登录失败: {account._login_error}")

    if account.token_store and account.token:
        await account.token_store.save_token(account.token)
        print("登录状态已保存")

    print("小米账号登录成功")

    service = MiNAService(account)
    devices = await service.device_list(master=1)

    if not devices:
        raise RuntimeError("未找到小爱音箱设备")

    print("找到以下设备:")
    for i, d in enumerate(devices):
        name = d.get("name", "未知")
        did = d.get("deviceID", "未知")
        model = d.get("model", "")
        hw = d.get("hardware", "")
        print(f"  [{i}] {name} ({model}) hardware={hw} - {did}")

    if config.DEVICE_ID:
        target_ids = [s.strip() for s in config.DEVICE_ID.split(",")]
        speakers = []
        for d in devices:
            if d.get("deviceID") in target_ids:
                speakers.append(XiaoAiSpeaker(
                    account, service,
                    d["deviceID"], d.get("hardware", ""), d.get("name", ""),
                ))
        if not speakers:
            raise RuntimeError(f"未找到配置的设备: {config.DEVICE_ID}")
        print(f"已连接 {len(speakers)} 台设备")
    else:
        speakers = [
            XiaoAiSpeaker(account, service, d["deviceID"], d.get("hardware", ""), d.get("name", ""))
            for d in devices
        ]
        print(f"已连接全部 {len(speakers)} 台设备")

    return speakers
