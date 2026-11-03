# 开发与真实媒体验收记录

日期：2026-11-03。工作目录：`D:\diy\try\逆向唧唧`。本记录对应本机一次实际开发验收；媒体、原始命令结果和会话记录保存在工作目录的 `.cache/video-provider/verification/`，不随可拷贝的 Skill 目录一起分发。

后续状态：2026-11-04，用户反馈“已验证通过”。以下保留 2026-11-03 的原始开发过程；其中“待手动验收”描述的是当时状态。本文件为本机开发记录，不纳入通用源码 ZIP。

## 实现与依赖

新增 `scripts/video_provider.py` 和 Skill 文档；保留根目录原有 `SKILL.md`、`video_provider.py` 及旧缓存。生产脚本只编排标准库、yt-dlp 和 FFmpeg，不调用模型或第三方解析服务。

- Python 3.12；CLI 要求 Python 3.10+。
- 项目便携版 [yt-dlp 2026.10.26](https://github.com/yt-dlp/yt-dlp/releases/tag/2026.10.26)，位于 `.cache/video-provider/tools/yt-dlp.exe`，实际执行过版本查询。
- 复用本机 FFmpeg/ffprobe 9.0.1，通过 PATH 或参数定位。
- 未安装全局依赖，未读取浏览器或唧唧Down 登录态；实测远程请求均为游客模式。

取流、签名、编码选择和 B 站字幕格式转换复用 [yt-dlp B 站提取器](https://github.com/yt-dlp/yt-dlp/blob/2026.10.26/yt_dlp/extractor/bilibili.py)。仅补充普通链接规范化、短链跳转和公开分 P 列表查询。

## 供片与边界检查

表中 JSON 文件名均相对于上述证据目录；文件保留完整输出，下面只摘录关键结果。

| 实际用例 | 结果 | 证据 |
| --- | --- | --- |
| 普通链接 `BV13eDABsEU6`，4.125–26.500 秒 | 获得 1080×1440、H.264/AAC MP4，实际容器时长 22.4 秒；额外音频成功 | `magnet-fetch.json` |
| 游客实际画质 | 同一来源返回并选中 `100026+30280`；来源视频编码 AV1，没有将游客清晰度固定为 360P/480P | `magnet-fetch.json`、`magnet-full.json` |
| `--full` | 33.266 秒 MKV，保留来源 AV1/AAC | `magnet-full.json` |
| 开头 28.125 秒、请求 10 秒 | 实际范围截至来源末尾 33.266 秒；直接使用完整来源缓存裁剪 | `tail-fetch.json` |
| 相同片段再次获取 | `cached: true`、`acquisition: cached_output` | `cache-hit.json` |
| 真实短码 `https://b23.tv/neeKfZN` | 解析为 `BV1Z5xK67Ei9`、CID 43089136517；随后取得原片 6–24 秒、1080×1920 MP4 | `short-current.json`、`charades-fetch.json` |
| 已失效的短码 | 返回 `SHORT_LINK_FAILED` | `short-inspect.json` |
| `av116342309063442` | 解析为 `BV13eDABsEU6`、CID 37219665941 | `final-remote-cases.json` |
| `BV1wD4y1o7AS` 的第 2 P | 查询到 200 P，选中 CID 1298431128；3.125–7.375 秒片段为 768×480、H.264/AAC，独立音频成功 | `final-remote-cases.json` |
| 无效第 9999 P | 非零退出，`INVALID_PAGE` | `invalid-page.json` |
| 4.125 与 4.875 秒起点 | 两个片段使用不同缓存；来源时间偏移正确累加 | `functional-cases.json` |
| 毫秒舍入和字幕裁切 | `19.0004` 秒归为 19000 ms，`1.4996` 秒归为 1500 ms；字幕跨两端裁切并归零为 0–1500 ms | `final-local-cases.json` |
| 缓存片段再次裁剪 | 字幕语言仍为 `en`；250–750 ms 映射回原来源 19250–19750 ms | `final-local-cases.json` |
| 本地来源 720×306，指定高度上限 480 | 输出仍为 720×306，没有放大 | `final-local-cases.json` |
| 来源无音轨 | 正常提供视频，独立音频状态为 `not_present` | `functional-cases.json` |
| 起点越界、NaN、`00:60:00` | 分别返回 `RANGE_OUT_OF_BOUNDS` 或 `INVALID_TIME`，非零退出 | `functional-cases.json`、`final-local-cases.json` |
| 原有 325 字节异常 MP4 | 返回 `INVALID_MEDIA`；旧文件保留 | `functional-cases.json` |
| 0.25 秒超时 | 非零退出 `TOOL_TIMEOUT`，目标缓存目录为空；正常重试生成有效媒体，`cached: false` | `functional-cases.json`、`final-local-cases.json` |
| 显式 FFmpeg 目录，子进程 PATH 为空 | 成功定位同目录 ffprobe，完成本地媒体查询 | `explicit-tool-directory.json` |
| 显式均匀采样 | 22.4 秒片段生成 12 张 JPEG，保留片段时间和来源时间；已查看代表帧长图 | `magnet-frames.json`、`keyframes_preview.jpg` |
| 独立 WAV | 18 秒 PCM S16LE，3175274 字节；解码退出 0，均值 -18.7 dB、峰值 -0.5 dB | `final-local-cases.json`、`audio-check.txt` |

时长受实际帧间隔影响；JSON 同时提供请求/实际逻辑区间及探测到的容器时长。

## 字幕的实际覆盖

游客查询 `BV1Sb41127UZ` 时，上游明确提示字幕需要登录；本次没有用户 Cookie，未取得其受登录限制的字幕。视频供给继续正常进行。证据为 `subtitle-inspect.json`，不能将这个结果写成“该视频没有字幕”。

裁切与重置时间轴使用真实公开的 [Sintel 视频片段](https://raw.githubusercontent.com/iandevlin/iandevlin.github.io/master/mdn/video-player-with-captions/video/sintel-short.mp4) 和 [配套字幕](https://raw.githubusercontent.com/iandevlin/iandevlin.github.io/master/mdn/video-player-with-captions/subtitles/vtt/sintel-en.vtt)，由 FFmpeg 将 VTT 转成同名 SRT 后交给 CLI。使用了实际对白对应的字幕区间。

字幕失败检查是把本任务生成的字幕文件临时移开，随后恢复：第一次返回视频成功、字幕 `failed`；恢复后再次获取复用视频缓存，字幕变为 `available`。另一个没有字幕内容的区间返回 `outside_range`。这验证了字幕读取失败的隔离及重试；远程字幕 HTTP 失败分支经静态审查，本次未制造上游服务故障。

显式 Cookie 参数的数据流已审查：原文件只读复制到临时目录，下载器和网络请求使用临时副本，结束时清理副本；本次未执行真实登录态测试。

## 媒体输入与理解

独立 Antigravity 会话：`da649e68-4bfd-4731-8b20-2e6e8f3e6035`。沿用当时客户端当前模型，未切换模型，未将其接入生产 CLI。两轮均只提供中性命名的 MP4 及问题，不提供标题、字幕、来源链接或预期答案。

| 阶段 | 本次事实 |
| --- | --- |
| 媒体取得 | 两条 MP4 均具有真实画面和 AAC 音轨，已核对轨道、时长及代表帧；第二条还独立导出并解码 WAV |
| 媒体读取 | 两轮均记录到对对应 MP4 的 `read_file` 调用，状态 `DONE`；第二轮出现与实际片段对应的具体画面回答 |
| 画面问答 | 第二轮描述了戴眼镜者鼓腮、雨衣、随后俯身和画面切换等；代表帧可核对这些具体内容。人数、站位等更细的回答未逐项确认，不将整份回答标为全部正确 |
| 声音问答 | 第二轮模型明确报告未收到音频，声音验收待用户手动完成 |

第一条片段在读取后触发 Gemini 过滤，虽然进程及 JSON 结果状态为 SUCCESS，实际没有完成内容回答。第二条总耗时约 633 秒；其回答没有证明听觉输入成功。根据用户要求，停止继续自动尝试，准备 [手动验收材料](manual-acceptance.md)。

证据位于 `native/`：`prompt.txt`、`prompt-b.txt`、`events.jsonl`、`events-b.jsonl`、`process.json`、`process-b.json`、`response.md`、`response-b.md`。事件日志记录了调用路径与状态，没有展开媒体附件本体；附件类型与声音通道由后续手动验收补充记录。

Codex 和 Pi 的首版行为遵循 [客户端能力说明](client-capabilities.md)，本次未将二者的视频路径读取冒充原生视频输入。

## 静态审查与交付状态

入口为 `main → inspect/fetch/frames`。远程格式和字幕交给 yt-dlp；片段通过 FFmpeg 转换，探测合格后才原子移动到正式缓存并写入结果；字幕和额外音频各自报告状态。缓存键包括来源身份、CID/分 P、实际选定格式及毫秒范围。生产路径没有 Agent、MCP、OCR、ASR 或额外理解模型调用。

已执行 Skill 格式验证，结果为 `Skill is valid!`。供片 MVP 和文档交付；Gemini 的听觉理解及完整问答验收转为用户手动进行。
