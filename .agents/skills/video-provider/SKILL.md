---
name: video-provider
description: 获取 Bilibili 普通链接、BV/AV 编号或 b23 短链对应的视频、音频和已有字幕，提供本地裁剪、格式转换及显式抽帧。媒体理解由当前 Agent 模型完成。
---

# 视频提供器

本 Skill 提供真实媒体文件及来源、时间、格式等事实。使用当前 Agent 的模型理解媒体；CLI 不调用任何模型、OCR、ASR、翻译或第三方解析服务。

## 先确定输入通道

用户需要分析视频时，先核对当前会话的工具声明、当前模型和客户端的媒体输入能力。不能凭工具叫 `read_file`、文件可访问或播放器可预览，就声称模型已收到视频。

- 有原生视频通道：获取 MP4，再通过该通道读取媒体。
- 原生视频通道不可用或无法确认：先报告能力限制。用户仍要求下载文件时，可以提供文件；用户明确选择图片模式时才运行 `frames`。
- Pi 首版只报告原生视频能力限制，不安装宿主扩展。

客户端差异与系统兼容情况见 [客户端能力说明](references/client-capabilities.md)。首次安装或依赖不可用时，读 [安装与依赖](references/setup.md)；需要确认媒体是否进入模型时，读 [手动验收步骤](references/manual-acceptance.md)。

## 调用 CLI

以本 Skill 目录为基准定位 `scripts/video_provider.py`，使用其绝对路径。命令的工作目录使用用户的项目目录；默认缓存位于该工作目录的 `.cache/video-provider/`。参数放在子命令后面。

```text
python <Skill绝对目录>/scripts/video_provider.py doctor --json
python <Skill绝对目录>/scripts/video_provider.py inspect <URL或BV/AV编号> --json
python <Skill绝对目录>/scripts/video_provider.py fetch <URL或本地视频> --start 00:02:00 --duration 30 --audio --json
python <Skill绝对目录>/scripts/video_provider.py fetch <URL或本地视频> --full --json
python <Skill绝对目录>/scripts/video_provider.py frames <本地视频> --count 12 --json
```

CLI 依赖 Python 3.10+ 标准库及外部 yt-dlp、FFmpeg、ffprobe，不自动安装或更新工具。同一会话首次使用或出现依赖错误时运行 `doctor`，检查工具、实际版本和必要编码器；返回 `dependencies_missing` 时，按宿主的权限规则准备依赖。查找顺序与各系统使用方式见 [安装与依赖](references/setup.md)。

| 参数 | 行为 |
| --- | --- |
| `--start` | 秒数或 HH:MM:SS，可含小数，默认 0 |
| `--duration` | 正时长；`fetch` 默认 120 秒，`frames` 默认采样到结尾 |
| `--full` | 获取完整视频，默认保留来源编码；不与区间参数混用 |
| `--page` | 正整数，显式值覆盖链接的 `p` 参数 |
| `--max-height` | 像素高度上限；远程选流遵守此上限，本地转换不放大来源 |
| `--audio` | 额外导出保留音频编码的 MKA 文件 |
| `--audio-format wav` | 提取 PCM WAV，适用于不接受 MKA 的原生音频工具；该参数同时启用音频提取 |
| `--cookies` | 显式提供 Netscape Cookie 文件；只将临时副本交给下载器 |
| `--count` | `frames` 均匀采样数，默认 12 |
| `--cache-dir` | 缓存和便携工具目录；跨工作目录调用时显式指定 |
| `--yt-dlp` / `--ffmpeg` / `--ffprobe` | 指定工具路径或所在目录 |
| `--timeout` | 单次外部工具调用的超时秒数，默认 600 |
| `--json` | 显式要求机器可读输出；当前 CLI 始终输出 UTF-8 JSON |

游客模式会选择服务器实际返回的最佳画质，不预设游客只能获得低清。`--max-height` 表示真实像素高度，不等同于平台的“720P”等画质标签；竖屏、横屏均按实际尺寸处理。

## 获取与读取

1. 用户已明确分 P 和时间区间时，直接 `fetch`，它会解析并检查来源；需要决定区间、分 P 或全片覆盖范围时，再用 `inspect` 查询时长和实际格式。查询结果是媒体事实，没有读取视频画面。
2. 根据问题选择 `fetch` 区间。普通片段输出 H.264/AAC MP4，保留音轨。起点越界报错，超过末尾的区间会明确返回截到结尾的实际范围。
3. 检查 `video_path`、`media.size_bytes`、轨道和实际区间，再使用当前客户端的原生视频工具读取。超出已知输入预算时缩短区间；可以对已下载的本地文件再次裁剪，避免重复下载。
4. 全片问题使用完整视频或连续片段覆盖时间轴。回答中说明实际分析区间，不把开头试看或字幕读取写成全片观看。
5. 模型依据收到的媒体回答。引用原视频时间时，使用 `actual_range.source_start_ms + 片段内时间`。本工具自己的缓存视频作为本地输入再次裁剪时，会沿用来源时间偏移。

视频工具只返回画面，而问题涉及声音时，检查同一会话是否还有原生音频通道；可对同一片段使用 `fetch <本地文件> --full --audio-format wav`，将独立 WAV 交给当前模型。回答时说明声音来自独立音频输入；原生音频也不可用时，报告该限制。

完整下载保留来源编码，容器可能是 MKV；客户端不接受该容器或编码时，再从本地来源生成 MP4 片段。完整来源已经缓存且选定格式一致时，片段直接从本地完整来源裁剪。

远程片段在区间下载的同一轮 FFmpeg 中完成精确裁剪与兼容编码；本地来源仍转换一次。CLI 复用本次解析的临时元信息，播放地址失效时交给 yt-dlp 按来源重新解析；临时信息不写入正式结果。

## 结果与缓存

`doctor` 返回 `status: ready` 或 `dependencies_missing`，后者非零退出。它检查本地工具，不查询视频或调用模型。

`inspect` 返回 `status: inspected`；`fetch` 返回 `status: provided`；`frames` 返回 `status: frames_provided`。这些状态均只陈述媒体事实。错误返回 `status: error`、`code` 和 `message`，进程非零退出。

`fetch` 结果的主要字段：

- `source`：视频身份、分 P、可得的 CID、规范链接或本地来源；不包含播放直链或 Cookie。
- `requested_range` / `actual_range`：请求区间、实际区间及原视频时间映射，单位毫秒。
- `video_path` / `audio_path`：绝对路径；`audio_status`、`audio_media` 单独反映独立音频是否生成及其实际格式。
- `media`：实际时长、尺寸、编码、像素格式、色彩范围、音轨和文件大小。
- `subtitles`：字幕语言、来源、可确定时的自动字幕标识、路径和状态；只获取已有字幕。
- `subtitle_status: not_returned`：此次未获得字幕，不据此断言平台永远没有字幕。
- `transformations` / `warnings`：实际转换及独立失败信息。

缓存使用来源身份、分 P/CID、实际选定格式和毫秒时间范围。命中缓存仍会查询远程格式及字幕；只有媒体文件被复用。失败文件不视为已完成缓存，字幕失败不阻断视频，也不会永久跳过后续字幕获取。

远程片段的单次转码结果使用 `h264-aac-v2` 缓存标识，旧片段保留；完整来源缓存可继续复用。兼容片段及其缓存均检查 H.264/AAC、8 位 4:2:0、MP4、尺寸与时长，保留视频原有的全范围或有限范围标识。

超时或取消时停止本次下载器及其子进程，再清理临时文件。依赖、媒体和缓存放在当前项目或显式指定的缓存目录；不要写入安装 Skill 的只读目录。

## 图片模式

仅在用户明确选择后调用 `frames`。将返回的 JPEG 作为图片内容交给当前模型，并保留每张图片的时间戳。它证明的是采样画面输入；不能据此声称读取了连续视频或听到了声音。
