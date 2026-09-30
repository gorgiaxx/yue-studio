# YuE Studio

[English](README.md) | [简体中文](README.zh.md)

本地优先的 macOS（Apple Silicon）Web 工作台：[YuE2](https://github.com/multimodal-art-projection/YuE) 歌曲生成、[SheetSage2](https://huggingface.co/m-a-p/SheetSage2) 音频转录、可视化 ABC 乐谱编辑器。uv 管理依赖，SQLite 持久化，全部离线运行。

---

## 架构

```
yue-studio/
├── app/
│   ├── main.py        FastAPI：完整 REST API（歌曲 / 转录 / 乐谱 / 取消 / 重命名）
│   ├── workers.py     后台线程：MPS 生成（协作式取消）
│   │                  + 转录子进程（可强杀）。中断任务在重启后
│   │                  自动恢复（running/pending → 重新入队）。
│   ├── db.py           SQLite 持久化（data/studio.db）
│   └── static/         单页 UI：主应用（创建 / 曲库 / 队列）+ 乐谱编辑器
├── scripts/
│   └── fetch_soundfonts.py   钢琴采样下载脚本（幂等，带缓存校验——见下文）
├── data/               SQLite 数据库（运行时创建，不入仓库）
├── outputs/            生成歌曲产物（audio.flac / score.abc / latent.npy …）
└── runs/               转录输出（score.abc / *.lab / *.mid）
```

两套模型环境必须分开（依赖版本冲突）：本仓库 `.venv`（uv，Python 3.12，torch 2.10 MPS）跑 YuE2 生成；`../YuE/.venv-ss2`（Python 3.11，transformers 4.45.2）由 worker 以子进程调用跑 SheetSage2 转录。模型权重共用 `../hf-cache`（YuE2-3B、YuE2-Vae、MERT2）。

## 快速开始

```bash
# 1. 首次运行：下载钢琴采样（约 2.1 MB；来源与授权见下文）
python3 scripts/fetch_soundfonts.py

# 2. 安装依赖（uv）
uv sync

# 3. 启动（转录功能还需要 ../YuE/.venv-ss2 —— 见 YuE 仓库 README）
uv run uvicorn app.main:app --host 127.0.0.1 --port 8770
# 打开 http://127.0.0.1:8770
```

## 功能

**创作** — 标题 / 风格提示词 / 歌词（段落标签 `[Intro] [Verse] [Pre-Chorus] [Chorus] [Bridge] [Outro]`；`[la]` 表示哼唱）/ seed。三种模式：*智能编曲*（AI 规划旋律与和声）、*旋律引导*（基于现有 ABC 乐谱重新编曲——从内置曲库下拉框选择）、*直接生成*（跳过乐谱规划）。

**乐谱工作流** — 乐谱属于转录；歌曲由乐谱派生：
```
音频文件 ─转录→ 🎼 乐谱（转录资产） ─派生→ 🎵 歌曲₁ 🎵 歌曲₂ …
                ↑ 在编辑器中精修（保存写回规范副本）
```
- 一份转录乐谱可派生任意数量的翻唱（选择器 / 编辑器“新建歌曲”对话框 / 主页按钮）
- 每首歌保留自己的乐谱快照，可独立编辑

**乐谱编辑器**（`/editor/trans/{id}` 或 `/editor/{sid}`）—
- 左侧 ABC 源码，右侧实时五线谱渲染。点击 / 进度条 / 播放的双向光标同步
- 试听预览：MIDI 钢琴合成 + 可拖动进度条 + 紫色音符高亮跟随播放头 + 代码窗格焦点同步 + 从光标处播放
- 专业快捷键：`←→` 音符导航、`↑↓` 半音调节（Shift = 八度）、`1–8` 时值、`R` 反复、`Backspace` 休止、`Space` 播放、`⌘Z / ⌘⇧Z` 撤销/重做、`?` 帮助面板
- 交互：拖拽音符改变音高，右键菜单（音高 / 八度 / 时值 / 休止 / 删除），双击试听

**任务管理** — 运行中/排队任务可停止（生成用协作式取消，转录强杀子进程）；已取消任务可重新入队；服务重启后中断任务自动恢复。

**持久化** — 所有状态存于 SQLite + 磁盘产物，重启不丢失。卡片显示创建/修改时间，双击重命名标题。

## 性能参考

M4 Pro（48 GB）实测：

- 转录约 97 秒音频：≈ 1.5 分钟
- 生成（旋律模式，96 秒输出）：≈ 5.5 分钟；直接模式（213 秒输出）：≈ 22 分钟
- 队列为串行（一次一个任务）

## API

```
POST   /api/songs                          创建生成任务 {title, style, lyrics, cot, abc?, seed}
GET    /api/songs, /api/songs/{id}         列表 / 详情
PATCH  /api/songs/{id}                     重命名 {title}
DELETE /api/songs/{id}                     删除（含产物）
POST   /api/songs/{id}/cancel              停止 / 出队
POST   /api/songs/{id}/regenerate          基于当前乐谱重新生成
PUT    /api/songs/{id}/abc                 保存乐谱快照
GET    /api/songs/{id}/audio               音频（FLAC）
POST   /api/transcriptions                 提交转录 {path}
GET    /api/transcriptions, /{id}          列表 / 详情
PATCH  /api/transcriptions/{id}            重命名
PUT    /api/transcriptions/{id}/abc        保存规范乐谱
POST   /api/transcriptions/{id}/cancel     停止
POST   /api/songs/from-transcription/{id}  由乐谱派生歌曲 {title, style, lyrics, seed}
GET    /editor/trans/{tid} | /editor/{sid} 乐谱编辑器页面
```

## 钢琴音源

编辑器试听需要 89 个钢琴采样（FluidR3_GM 原声三角钢琴，约 2.1 MB）。**采样不入仓库**，由脚本按需下载到 `app/static/soundfonts/`（已 gitignore）：

```bash
python3 scripts/fetch_soundfonts.py          # 下载 / 补齐缓存（幂等）
python3 scripts/fetch_soundfonts.py --check  # 仅校验；退出码可用于 CI
```

- 编辑器播放前探测缓存，缺失时明确提示（不静默失败）
- 脚本精确下载 abcjs 请求的音符（黑键用降号命名：Bb/Db/Eb/Gb/Ab）

受限网络下脚本无法运行时，可在任意联网机器上运行后整体拷贝 `app/static/soundfonts/` 目录。

### 素材来源与授权

| 素材 | 来源 | 授权 |
|---|---|---|
| FluidR3_GM acoustic_grand_piano-mp3 | [midi-js-soundfonts](https://github.com/paulrosen/midi-js-soundfonts)（预转换镜像） | **MIT**；源自 FluidR3_GM SoundFont（FluidSynth 社区） |
| abcjs（乐谱渲染/合成 JS 库，内置于 `app/static/vendor/`） | [abcjs](https://github.com/paulrosen/abcjs) | **MIT** |
| YuE2 推理运行时 | [multimodal-art-projection/YuE](https://github.com/multimodal-art-projection/YuE) | Apache-2.0（权重见其 MODEL_LICENSE） |
| YuE2-3B / YuE2-Vae / SheetSage2 / MERT2 权重 | [m-a-p on Hugging Face](https://huggingface.co/m-a-p) | 见各模型卡 |

## 开源声明

本仓库自身代码以 **Apache License 2.0** 发布（见 [LICENSE](LICENSE)）。第三方组件及其授权：

- **YuE**（Apache-2.0）— 歌词到歌曲的生成流水线与符号规划；模型权重遵循其 MODEL_LICENSE，仅本地推理
- **SheetSage2 / MERT2**（见各模型卡）— 音频到旋律/和弦/节拍的转录
- **abcjs**（MIT）— 五线谱渲染、交互、MIDI 合成
- **midi-js-soundfonts / FluidR3_GM**（MIT）— 试听采样
- **FastAPI / uvicorn / Pydantic**（MIT/BSD）— Web 服务
- **PyTorch**（BSD 风格）— MPS 推理

生成内容（音乐）的权利归属由底层模型许可决定——使用前请阅读各模型卡。本仓库不主张对生成产物的任何权利。

## 数据与隐私

全部处理在本机完成：SQLite、音频产物与模型缓存均在本地磁盘。无遥测、无外报。唯一的外部网络访问是首次运行 `fetch_soundfonts.py` 从 GitHub Pages 下载音源。
