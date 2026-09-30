# YuE Studio

macOS（Apple Silicon）本地 Web 工作台：[YuE2](https://github.com/multimodal-art-projection/YuE) 歌曲生成 + [SheetSage2](https://huggingface.co/m-a-p/SheetSage2) 音频转录 + 可视化 ABC 乐谱编辑器。uv 管理依赖，SQLite 持久化，全部离线运行。

## 架构

```
yue-studio/
├── app/
│   ├── main.py        FastAPI：歌曲/转录/乐谱/取消/重命名 全套 REST API
│   ├── workers.py     后台线程：MPS 生成（可协作取消）+ 转录子进程（可 kill）
│   │                  启动时自动恢复中断任务（running/pending → 重新入队）
│   ├── db.py          SQLite 持久化（data/studio.db）
│   └── static/        单页 UI：主页（创建/列表/队列）+ 乐谱编辑器（editor.html）
├── scripts/
│   └── fetch_soundfonts.py   钢琴音源下载脚本（幂等、带缓存校验，见下）
├── data/              SQLite 数据库（运行时生成，不入库）
├── outputs/           生成歌曲 artifacts（audio.flac/score.abc/latent.npy…）
└── runs/              转录输出（score.abc/*.lab/*.mid）
```

两套模型环境必须分开（依赖版本冲突）：本仓库 `.venv`（uv, Python 3.12, torch 2.10 MPS）跑 YuE2 生成；`../YuE/.venv-ss2`（Python 3.11, transformers 4.45.2）由 worker 以子进程调用跑 SheetSage2。模型权重共用 `/Volumes/intel760p/music_projects/hf-cache`（YuE2-3B、YuE2-Vae、MERT2）。

## 快速开始

```bash
# 1. 首次：拉取钢琴音源缓存（约 2.1MB，来源与授权见下方声明）
python3 scripts/fetch_soundfonts.py

# 2. 安装依赖（uv）
uv sync

# 3. 启动（另需 ../YuE/.venv-ss2 转录环境，见 YuE 仓库 README）
uv run uvicorn app.main:app --host 127.0.0.1 --port 8770
# 打开 http://127.0.0.1:8770
```

## 功能总览

**创建**（Create 页）：标题/风格/歌词（`[Intro][Verse][Pre-Chorus][Chorus][Bridge][Outro]` 段落标记，`[la]` 哼鸣）/Seed；三种模式——Smart Arrangement（AI 自动编曲）、Melody Guided（已有 ABC 谱 + 重新配乐，可选下拉填入库中已有乐谱）、Direct（跳过乐谱规划）。

**乐谱工作流**（谱属于转录，歌曲从谱衍生）：
```
音频文件 ──转录──> 🎼 乐谱（转录资产，双击可改名）──衍生──> 🎵 歌曲₁ 🎵 歌曲₂ …
                     ↑ 编辑器精修（保存即写回正本）
```
- 一个转录谱可生成任意多首翻唱（选谱/编辑器"生成新歌曲"/主页按钮三条路径）
- 歌曲列表可显示每首歌自己的谱快照并单独编辑

**乐谱编辑器**（`/editor/trans/{id}` 或 `/editor/{sid}`）：
- 左 ABC 源码 / 右五线谱实时渲染，双向光标联动（点击/拖动进度条/播放四处同步）
- 播放预览：MIDI 钢琴合成 + 进度条拖拽 seek + 谱面紫色高亮跟随 + 代码区焦点同步 + 从光标位置起播
- 专业编辑快捷键：`←→` 音符导航、`↑↓` 半音（`Shift` 八度）、`1-8` 时值、`R` 重复、`Backspace` 变休止、`空格` 播放、`⌘Z/⌘⇧Z` 撤销重做、`?` 帮助面板
- 交互：拖拽音符改音高、右键菜单（音高/八度/时值/休止/删除）、双击试听

**任务管理**：running/pending 任务可「停止」（生成走协作取消、转录 kill 子进程）；cancelled 可「重新排队」；服务重启自动恢复中断任务。

**持久化**：全部状态存 SQLite + 磁盘 artifacts，重启不丢；列表显示创建/修改时间，标题双击重命名。

## 性能参考（M4 Pro 48GB）

- 转录 ~97s 音频：约 1.5 分钟
- 生成 melody 模式（96s 输出）：约 5.5 分钟；direct 模式（213s 输出）：约 22 分钟
- 队列串行（一次一个任务）

## API

```
POST   /api/songs                          创建生成任务 {title, style, lyrics, cot, abc?, seed}
GET    /api/songs / GET /api/songs/{id}    列表/详情
PATCH  /api/songs/{id}                     重命名 {title}
DELETE /api/songs/{id}                     删除（含 artifacts）
POST   /api/songs/{id}/cancel              停止运行/出队
POST   /api/songs/{id}/regenerate          用当前谱重新生成
PUT    /api/songs/{id}/abc                 保存谱快照
GET    /api/songs/{id}/audio              音频 (FLAC)
POST   /api/transcriptions                 提交转录 {path}
GET    /api/transcriptions …              列表/详情
PATCH  /api/transcriptions/{id}            重命名
PUT    /api/transcriptions/{id}/abc        保存谱正本
POST   /api/transcriptions/{id}/cancel    停止
POST   /api/songs/from-transcription/{id} 从谱建新歌 {title, style, lyrics, seed}
GET    /editor/trans/{tid} | /editor/{sid} 乐谱编辑器
```

## 钢琴音源（重要）

乐谱编辑器的 MIDI 试听需要 89 个钢琴音符采样（FluidR3_GM acoustic grand piano，共约 2.1MB）。**素材不随仓库分发**，由脚本按需下载到 `app/static/soundfonts/`（已 gitignore）：

```bash
python3 scripts/fetch_soundfonts.py          # 下载/补全缓存（幂等，已有文件跳过）
python3 scripts/fetch_soundfonts.py --check  # 仅校验完整性，退出码可用于 CI
```

- 编辑器在播放前会探测缓存，缺失时明确提示运行本脚本，而不是静默失败
- 脚本按 abcjs 的音符命名表（黑键用降号名：Bb/Db/Eb/Gb/Ab）精确拉取，不多下一个文件

### 素材来源与授权

| 素材 | 来源 | 许可 |
|---|---|---|
| FluidR3_GM acoustic_grand_piano-mp3 | [midi-js-soundfonts](https://github.com/paulrosen/midi-js-soundfonts)（paulrosen 维护的预转换镜像） | **MIT**，源自 FluidSynth 社区的 FluidR3_GM SoundFont |
| abcjs（乐谱渲染/合成 JS 库，已 vendor 于 `app/static/vendor/`） | [abcjs](https://github.com/paulrosen/abcjs) | **MIT** |
| YuE2 推理运行时 | [multimodal-art-projection/YuE](https://github.com/multimodal-art-projection/YuE) | Apache-2.0（模型权重另见其 MODEL_LICENSE） |
| SheetSage2 / MERT2 / YuE2-3B / YuE2-Vae 模型权重 | [m-a-p @ Hugging Face](https://huggingface.co/m-a-p) | 见各模型卡 |

若在网络受限环境运行脚本失败，可在任意联网机器上执行后把 `app/static/soundfonts/` 整目录拷贝过来。

## 开源声明

本仓库代码以 **MIT License** 发布（见 [LICENSE](LICENSE)）。使用到的第三方组件及其许可汇总：

- **YuE**（Apache-2.0）：歌词→歌曲生成管线与符号规划；模型权重遵循其 MODEL_LICENSE，仅限本地推理用途
- **SheetSage2 / MERT2**（见模型卡）：音频→旋律/和弦/拍号转录
- **abcjs**（MIT）：五线谱渲染、交互与 MIDI 合成
- **midi-js-soundfonts / FluidR3_GM**（MIT）：试听音色采样
- **FastAPI / uvicorn / Pydantic**（MIT/BSD）：Web 服务
- **PyTorch**（BSD-style）：MPS 推理

生成内容（音乐）的权利归属由底层模型许可决定，使用前请阅读各模型卡；本仓库不主张对生成产物的任何权利。

## 数据与隐私

全部处理在本机完成：SQLite、音频 artifacts、模型缓存均在本地磁盘，无遥测、无外部上报。唯一的外部网络访问是首次运行 `fetch_soundfonts.py` 时从 GitHub Pages 下载音源。
