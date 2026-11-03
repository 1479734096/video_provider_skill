---
name: video-provider
description: 提取 Bilibili 视频流与切片文件，供 Agent 自身多模态模型读取理解。
---

# Video Provider Skill

## 目标与职责

- 职责定位：提供视频切片文件与字幕数据，排除内置理解模型。
- 模型交互：Agent 通过自身模型输入通道读取视频切片，完成画面感知与语音理解。

## 运行环境与依赖

- 解释器：Python 3.12
- 依赖库：requests
- 外部工具：FFmpeg（路径：`C:\Users\lenovo\AppData\Local\Programs\Python\Python312\Lib\site-packages\imageio_ffmpeg\binaries\ffmpeg-win-x86_64-v7.1.exe`）

## CLI 工具调用规范

脚本位置：`video_provider.py`

### 命令行语法

```bash
python video_provider.py <URL_OR_BVID> [OPTIONS]
```

### 参数定义

| 参数名 | 类型 | 默认值 | 规范与限制 |
| :--- | :--- | :--- | :--- |
| `<URL_OR_BVID>` | 文本 | 必填 | 支持 Bilibili 视频链接、b23 短链接、BV 编号 |
| `--start` | 文本/浮点数 | 0 | 切片起始秒数或 HH:MM:SS 时间点 |
| `--duration` | 浮点数 | 15.0 | 切片时长，数值限制区间为 [15.0, 60.0] 秒 |
| `--resolution` | 枚举 | 480p | 视频分辨率，限定选项：`360p`、`480p` |
| `--cache-dir` | 路径 | .cache | 缓存目录路径 |
| `--force` | 布尔开关 | 假 | 忽略本地缓存，重新执行网络下载与转码 |
| `--json` | 布尔开关 | 假 | 以 JSON 字符串格式输出执行结果 |
| `--ffmpeg` | 路径 | 系统路径 | 指定 FFmpeg 二进制程序位置 |

### JSON 输出字段定义

```json
{
  "status": "success",
  "cached": true,
  "title": "视频标题",
  "bvid": "BV1GJ411x7h7",
  "p": 1,
  "start": 0.0,
  "duration": 15.0,
  "resolution": "480p",
  "video_path": "D:\\diy\\try\\逆向唧唧\\.cache\\BV1GJ411x7h7_p1_ss0_t15_480p.mp4",
  "subtitle_path": null
}
```

- `status`：执行状态，取值 `success` 或 `error`。
- `cached`：布尔值，表示结果来自缓存或新增转码。
- `video_path`：切片 MP4 文件绝对路径。
- `subtitle_path`：切片时间区间对应 SRT 字幕文件路径；无字幕时取值 null。

## Agent 执行工作流

1. **解析请求**：从用户提问提取视频 URL、目标时间点与分析意图。
2. **生成切片**：运行 `python video_provider.py <URL> --start <START> --duration <DURATION> --json`。
3. **读取切片**：调用 Agent 文件读取工具（如 `view_file` 或 `read_file`）载入目标 `video_path`。
4. **模型推理**：Agent 依靠自身视觉与音频解析能力分析画面内容并回答用户提问。

## 缓存管理机制

- 存储目录：工作区 `.cache/`。
- 命名规则：`{bvid}_p{p}_ss{start}_t{duration}_{resolution}.mp4`。
- 复用逻辑：命中匹配文件且体积大于 0 字节时，直接返回已有路径，规避网络请求与转码损耗。
