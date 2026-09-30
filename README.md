# YuE Studio

macOS (Apple Silicon) Web UI for [YuE2](https://github.com/multimodal-art-projection/YuE) 歌曲生成与 [SheetSage2](https://huggingface.co/m-a-p/SheetSage2) 音频转录。uv 管理依赖，SQLite 持久化所有歌曲与转录记录。

## 架构

```
app/main.py      FastAPI 服务 (端口 8770)
app/workers.py   后台线程队列: 生成 (MPS) + 转录 (子进程调 .venv-ss2)
app/db.py        SQLite 持久化 (data/studio.db)
app/static/      单页 Web UI
outputs/         生成的歌曲 artifacts (audio.flac, score.abc, latent.npy ...)
runs/            转录输出 (score.abc, *.lab, *.mid)
```

两套模型环境必须分开（依赖版本冲突）：本项目的 `.venv`（uv, Python 3.12, torch 2.10）跑 YuE2 生成；`../YuE/.venv-ss2`（Python 3.11, transformers 4.45.2）由 worker 以子进程方式调用跑 SheetSage2 转录。

## 运行

```bash
cd /Volumes/intel760p/music_projects/yue-studio
uv run uvicorn app.main:app --host 127.0.0.1 --port 8770
# 打开 http://127.0.0.1:8770
```

模型缓存共用 `/Volumes/intel760p/music_projects/hf-cache`（YuE2-3B、YuE2-Vae、MERT2）。

## 使用

**生成歌曲**：填标题、风格提示词、歌词（`[verse]/[chorus]/[bridge]` 分段，`[la]` 为哼鸣），选模式后提交。任务入队，约 5–20 分钟出 48kHz 立体声 FLAC，列表内直接播放。

模式说明：
- `full`（智能编曲）：YuE2 自动写旋律+和弦乐谱再渲染 — 默认，不需要 ABC。
- `melody`（旋律模式）：提供固定旋律 ABC，YuE2 重新配乐 — 用于翻唱。
- `off`（直出）：跳过乐谱规划，直接从歌词+风格生成（CFG 双分支，更慢）。

**转录音频**：提交本机音频路径（如 `/Users/rin/Documents/Audacity4/duyiwuer.mp3`），SheetSage2 转出旋律 ABC、调性、时长。完成后点“用这个旋律生成翻唱 →”，ABC 自动填入生成表单，填上新歌词/风格即成翻唱。

**ABC 是什么**：记录旋律（和可选和弦）的文本乐谱格式。生成时它决定音符走向；转录得到它就能让新编曲沿用原曲旋律。不懂可完全忽略——只用 `full` 模式即可。

## 性能参考 (M4 Pro, 48GB)

- 转录 97s 音频：约 1.5 分钟
- 生成（melody 模式, 96s 输出）：约 5.5 分钟
- 生成（off 模式, 213s 输出）：约 22 分钟
- 队列串行执行，一次一个任务；刷新 3s 轮询。

## API

```
POST   /api/songs                        提交生成 {title, style, lyrics, cot, abc?, seed}
GET    /api/songs                        列出歌曲（含状态）
GET    /api/songs/{id}                   单曲详情
GET    /api/songs/{id}/audio             音频文件 (FLAC)
DELETE /api/songs/{id}                   删除（含 artifacts）
POST   /api/transcriptions               提交转录 {path}
GET    /api/transcriptions               列出转录
POST   /api/songs/from-transcription/{id} 从转录建翻唱任务 {title, style, lyrics, seed}
```
