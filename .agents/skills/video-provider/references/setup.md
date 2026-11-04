# 安装与依赖

## 安装 Skill

解压后，将完整的 `video-provider/` 文件夹放到项目的 `.agents/skills/` 中：

```text
项目目录/
└── .agents/skills/video-provider/
    ├── SKILL.md
    ├── scripts/video_provider.py
    └── references/
```

三种客户端当前均支持这个项目目录入口：[Codex](https://learn.chatgpt.com/docs/build-skills)、[Pi](https://github.com/badlogic/pi-mono/blob/main/packages/coding-agent/docs/skills.md)、[Antigravity](https://antigravity.google/docs/skills)。安装后按客户端要求刷新 Skill 或开启新会话。已有同名目录时先检查现有内容，合并或更新按用户授权进行。

也可以安装到客户端自己的用户级 Skill 目录。脚本路径始终从已加载的 Skill 所在位置解析；命令工作目录使用待处理视频所属项目。

## 准备运行环境

| 依赖 | 用途 | 版本说明 |
| --- | --- | --- |
| Python，推荐 3.12+ | 运行标准库 CLI | CLI 本身兼容 3.10+；Windows 已使用 3.12 完成历史实测；macOS/Linux 通常使用 `python3` |
| yt-dlp | B 站取流、字幕和区间下载 | 已实测基线 2026.10.26；保留现有工具，不自动升级 |
| FFmpeg 和 ffprobe | 转码、裁剪、抽帧及轨道探测 | 已实测 9.0.1；FFmpeg 需要 `libx264`、`aac`、`mjpeg` 编码器 |

yt-dlp 2026.10.26 的 [发布说明](https://github.com/yt-dlp/yt-dlp/releases/tag/2026.10.26) 要求 Python 3.11+。使用 Python 包或依赖系统 Python 的 Unix 可执行文件时，其运行时也须满足该要求；自带运行时的独立二进制由其自身提供。统一使用 Python 3.12+ 可以同时满足 CLI 和下载器。

本源码包不含二进制工具。优先复用已有安装；需要安装或下载时，按宿主 Agent 和用户的权限规则处理。可从 [Python](https://www.python.org/downloads/)、[yt-dlp 发布页](https://github.com/yt-dlp/yt-dlp/releases/tag/2026.10.26)、[FFmpeg 下载页](https://ffmpeg.org/download.html) 获取适合操作系统和 CPU 架构的工具。不修改全局环境也可以使用项目内的便携工具。

可执行文件按以下顺序查找：

1. 命令参数 `--yt-dlp`、`--ffmpeg`、`--ffprobe`；支持可执行文件或所在目录。
2. 环境变量 `VIDEO_PROVIDER_YT_DLP`、`VIDEO_PROVIDER_FFMPEG`、`VIDEO_PROVIDER_FFPROBE`。
3. 对 ffprobe，补查显式参数或环境变量指定的 FFmpeg 目录。
4. `<cache-dir>/tools/`：Windows 查找 `yt-dlp.exe`、`ffmpeg.exe`、`ffprobe.exe`；macOS/Linux 查找无 `.exe` 后缀的同名可执行文件。
5. PATH。

使用官方发布页上的其他文件名时，通过参数指定实际文件，不必重命名。macOS/Linux 的便携工具需要可执行权限。

## 依赖预检

在项目目录执行。以下示例对应项目级安装，用户级安装时改用脚本的实际绝对路径。

Windows：

```powershell
python ".agents/skills/video-provider/scripts/video_provider.py" doctor --json
```

macOS/Linux：

```sh
python3 ".agents/skills/video-provider/scripts/video_provider.py" doctor --json
```

如果 Python 在 Windows 上通过 `py -3` 提供，使用该入口替代 `python`。只需基础运行环境，无需为本脚本安装 pip 包。

`doctor` 输出实际工具路径、版本、Python/系统标识及必要编码器。依赖齐备时返回 `ready` 和退出码 0；缺少工具、无法启动或缺少编码器时返回 `dependencies_missing` 和退出码 1，每项含具体原因。它会初始化项目缓存目录，不联网、不下载媒体、不安装或更新工具。

CLI 继续接受显式提供的其他工具版本；yt-dlp 与已实测基线不同时会提示实际版本。遇到平台接口变化时由使用者决定更新，不在运行中自动升级。

## 缓存、路径与凭据

默认缓存是命令工作目录下的 `.cache/video-provider/`。跨工作目录复用工具与媒体时，对每次调用指定同一个 `--cache-dir`。媒体结果返回本机绝对路径；转移项目后重新生成或获取缓存，不把旧结果 JSON 中的绝对路径作为可移植引用。

明确的时间区间可以直接使用 `fetch`，无需先调用 `inspect`。非法时间与参数冲突在联网和初始化缓存前报错；源时长相关的越界检查在解析后完成。完整远程来源下载后直接验证并移入缓存，本地用户原文件会保留。

仅显式的 `--cookies <文件>` 会使用登录态。文件需为 Netscape 格式，脚本将临时副本交给下载器；不自动读取浏览器、其他客户端或唧唧Down 的账号数据。

`doctor` 仅做本地依赖检查。原生视频、图片和音频输入是否可用，需要由当前客户端的实际通道确认，见 [客户端能力说明](client-capabilities.md)。
