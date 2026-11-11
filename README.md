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

### 2. 接口约定

本项目只用到它的两个接口，都是纯 HTTP：

```
GET /music/search?q=关键词&source=migu&type=song&page=1   返回搜索结果页（HTML）
GET /music/download?id=...&source=...&stream=1            返回音频流
GET /music/inspect?id=...&source=...                      返回解析结果（JSON）
```

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

### 4. 前置条件

- 音箱和电脑同一局域网，防火墙要放行 Python 的监听端口（程序会自动起一个 HTTP 服务给音箱拉流，端口随机）
- 不是所有歌都能拿到，会员或版权受限的通常只有试听
- 音频来自公开渠道，仅供自己听，不要二次分发

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
