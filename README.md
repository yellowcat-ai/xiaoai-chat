# xiaoai-chat

让小爱音箱接入 AI 大模型，用语音和 AI 对话。

## 原理

通过小米云端 API 轮询小爱音箱的对话记录，截获用户提问，发送给大模型处理后，用 edge-tts 生成语音，通过音乐通道回传音箱播放。

## 支持的大模型

任何兼容 OpenAI API 格式的大模型都可以：

- DeepSeek（默认，成本极低）
- 通义千问
- OpenAI
- 其他兼容接口

## 准备材料

- 小爱音箱（已验证：Play 增强版 L05C）
- 一台同局域网的电脑（Windows / Mac / Linux）
- Python 3.10+
- 大模型 API Key（DeepSeek 充值 10 元可用很久）
- （可选）go-music-dl，用于音乐点播，见下文

## 部署步骤

### 1. 克隆项目

```bash
git clone https://github.com/wuyoucheng/xiaoai-chat.git
cd xiaoai-chat
```

### 2. 安装依赖

```bash
pip install -r requirements.txt
```

### 3. 配置

复制 `.env.example` 为 `.env`，填入你的信息：

```bash
cp .env.example .env
```

编辑 `.env`：

```
# 小米账号（手机号）
MI_USER=你的小米账号
MI_PASS=你的密码

# 大模型 API
LLM_API_BASE=https://api.deepseek.com/v1
LLM_API_KEY=你的API_KEY
LLM_MODEL=deepseek-chat
```

### 4. 启动

```bash
python main.py
```

首次启动会要求小米账号验证码，按提示操作即可。登录状态会保存到 `.mi.token`，后续启动不需要重复验证。

## 音乐点播（可选）

聊天功能不依赖这一步。想让它"说歌名就放整首"，需要再跑一个 [go-music-dl](https://github.com/guohuiyuan/go-music-dl)。

### 1. 启动 go-music-dl

从 Releases 下载对应平台的包（需要 v1.1.4 以上，选歌时会用到 `/music/inspect` 接口），Windows 解压后直接运行：

```bash
music-dl.exe web --no-browser
```

`--no-browser` 是不自动弹浏览器。默认监听 `http://localhost:8080/music`，浏览器能打开搜索页就说明好了。要常驻可以放 Docker，或者注册成系统服务开机自启。

本仓库自带的做法是 `watchdog.ps1` + Windows 计划任务（任务名 `XiaoAiChatWatchdog`，每分钟巡检一次）：两个进程都在它的托管范围内，music-dl 掉了会重新拉起，主程序掉了也会。夜间 23:00–07:00 主动停掉主程序，避免半夜自己续播。

### 2. 接口约定

本项目只用到它的三个接口，都是纯 HTTP：

```
GET /music/search?q=关键词&type=song&page=1                返回搜索结果页（HTML，各平台聚合）
GET /music/download?id=...&source=...&stream=1             返回音频流
GET /music/inspect?id=...&source=...                       返回解析结果（JSON）
```

注意 go-music-dl 1.1.x 的搜索是**多平台聚合**：一次请求就返回 migu / qq / kuwo / qianqian / netease 的混合结果，`source` 参数不再生效。所以 `music_search.py` 只请求一次，再按每条结果的 `data-source` 在本地分平台筛选，并按 60 秒 TTL 缓存、合并并发请求。

`/music/inspect` 用来确认选中的歌真能放、不是只有几秒的试听片段，接口不可用时自动退回字节探测。

默认连 `http://localhost:8080/music`，定义在 `music_search.py` 的 `MUSIC_DL_LOCAL`。改端口或放到另一台机器上，改这个常量即可。

### 3. 用法

对着音箱说"放周杰伦的歌"、"来点陈奕迅"、"下一首"、"暂停"、"停止播放"，或者"音量调到 40"。

- 并发搜咪咕、网易云、QQ、酷狗、酷我、千千，跨源投票挑原唱，只留能直接切片的 MP3
- 4 秒内没等到靠谱候选时会再等 3 秒慢源，避免挑到翻唱
- 某个源打不开时自动换平台重试（每场最多两次），不会直接跳过整首
- 第三方链接有 60 秒上限，所以一首歌会切成约 50 秒一段依次推送，按音箱真实拉流时刻对齐
- 歌单播放时会预取下一首，切歌不需要重新等待
- 听过的歌缓存在 `song_cache/`（默认 7 天、400MB），再点一次跳过搜索
- 说"单曲循环播放小兔子乖乖"或"小兔子乖乖单曲循环"会循环播放，说"取消循环"则当前这首播完停止
- "灌篮高手主题曲"这类描述性说法会先让大模型推断正式歌名（如《好想大声说爱你》）再搜索；普通歌名直接搜，不额外调用；推断结果缓存在 `song_intent_cache.json`

### 4. 本地点播库（可选）

儿歌、故事这类在线源经常搜不准（同名翻唱、音源张冠李戴），可以直接放本地文件，播放时完全不查在线搜索。

把文件放到项目的 `local_music/` 目录，**文件名就是点播词**：

```
local_music/小兔子乖乖 - 贝乐虎儿歌.mp3
local_music/数鸭子 - 贝乐虎儿歌.mp3
```

- 说"播放小兔子乖乖"、"贝乐虎儿歌的小兔子乖乖"都会命中第一条（带前缀、带"这首歌"的说法也认）
- 文件名写成 `歌名 - 歌手` 时，"歌手"会记进播放信息，日志里也能看到
- 支持 mp3 / flac / wav；flac、wav 会自动转码后切片
- 本地命中优先于缓存和在线搜索，不会因为网络或音源问题放错歌
- 本地歌曲同样支持"单曲循环播放…"和"取消循环"

### 5. 前置条件

- 音箱和电脑同一局域网，防火墙要放行 Python 的监听端口（程序会自动起一个 HTTP 服务给音箱拉流，端口随机）
- 不是所有歌都能拿到，会员或版权受限的通常只有试听；代码会按时长识别 30 秒试听片段并换源，实在没有完整版就不放，避免放一半突然停
- 音频来自公开渠道，仅供自己听，不要二次分发

## 播报接口（可选）

主程序会在本地 `8787` 端口开一个带 token 的 HTTP 接口，让脚本、手机快捷指令主动叫音箱说话，不用先喊"小爱同学"。

token 存放在 secrets-manager，名字是 `xiaoai_notify_token`（没有就跳过这个服务启动，不影响聊天和点歌）。取名用：

```bash
node ~/.secrets-manager/secrets.js get xiaoai_notify_token
```

调用示例：

```bash
curl -X POST http://localhost:8787/notify \
  -H "X-Notify-Token: $TOKEN" -H "Content-Type: application/json" \
  -d '{"text": "该吃饭了", "device": "客厅"}'
```

| 字段 | 说明 |
| --- | --- |
| `text` | 要播报的文字，最长 300 字；和 `audio_url` 二选一 |
| `audio_url` | 直接播放一个 http(s) 音频地址 |
| `device` | 设备名子串（客厅/主卧）或 device_id，不填默认客厅 |
| `interrupt` | 默认 `true`，会先停掉正在放的音乐；传 `false` 则直接抢占 |

`GET /healthz` 同样需要 token，用来确认服务和设备在线。

出门在外用手机流量访问：电脑上跑 `cloudflared tunnel --url http://localhost:8787`，会得到一个临时的 `https://xxx.trycloudflare.com` 地址，把它配进手机快捷指令（GET/POST + `X-Notify-Token` 头）即可。临时地址每次重启都会变；要固定地址需要 Cloudflare 账号和一个域名，用命名隧道（`cloudflared tunnel login` → `tunnel create` → `route dns`）。

## 已知限制

- 云端对话 API 有约 24 秒延迟，用户会先听到小爱自带的回答，再听到大模型的回答
- edge-tts 在 Windows 上可能有 IPv6 连接问题，代码已做 IPv4 强制处理
- 播放/暂停/下一首/停止/音量这些语音指令由本项目接管，其他设备控制类指令仍交给小爱内置 AI
- 咪咕部分歌曲只有无损源，下载后需要先转码才能切片，首次播放会多等 6~10 秒
- `music-dl` 的咪咕音质后缀（HQ/SQ/LQ）在部分版本里不生效，遇到 SQ 拿到 FLAC 时由本项目按帧判断后转码

## 技术细节

- **语音输入**：小米云端对话历史 API 轮询
- **大模型**：OpenAI 兼容接口（DeepSeek / 通义千问 / ChatGPT 等）
- **语音输出**：edge-tts（微软免费 TTS）生成 MP3 → 本地 HTTP 服务器 → `play_by_music_url` 音乐通道播放
- **音乐搜索**：go-music-dl 的 `/music/search`（HTML 结果页）→ 解析歌曲卡片 → 跨源按标题相似度、完整时长、歌手在多源的出现票数排序
- **音源选择**：对候选发 `Range: bytes=0-0` 探 `Content-Type`，只要 `mpeg`，按 咪咕→酷我→QQ→酷狗→网易云→千千 的顺序取第一个可用版本；选中的那首再调一次 `/music/inspect` 确认不是试听片段
- **换源重试**：`/music/switch_source` 按歌名+歌手换到另一个平台，换回来的结果必须同名、歌手有交集、`inspect` 完整可播才采用
- **切片**：直接解析 MPEG 帧头累计时长，在帧边界切成约 50 秒一段，不依赖 ffmpeg；边下边切，首段不用等整首下完
- **无缝连播**：本地 HTTP 服务记录音箱每次 GET 的时刻，用这个真实拉流时间给下一段排期，提前 0.7 秒推送
- **歌单与切歌**：一首歌播放期间预取下一首的切片队列，"下一首"直接把新分段推上去，不发静音占位
- **循环播放修复**：聊天回复播完后发送静音 MP3 覆盖，防止音乐通道循环
- **多设备支持**：自动发现账号下所有小爱音箱，也可通过 `DEVICE_ID` 指定

## License

MIT
