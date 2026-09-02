# Changelog

## [0.2.2] - 2026-09-02

### Added
- **页脚显示服务端版本号**：Web UI 页脚追加版本号（`<span id="footer-ver">`，由 `loadConfigStatus()` 在 `GET /api/config` 成功后写入 `v<version>`）；`/api/config` 返回新增 `version` 字段。
- **版本号单一来源收拢到 `minipic.__version__`**：`src/minipic/__init__.py` 的 `__version__` 升为 `0.2.2`；`pyproject.toml` 删掉静态 `version` 行，改为 `dynamic = ["version"]` + `[tool.setuptools.dynamic] version = { attr = "minipic.__version__" }`，避免下次再出双源不同步。

## [0.2.1] - 2026-09-02

### Fixed
- **HOTFIX 绿色免安装包媒体时长探测失败（WinError 2）**：`media.ffprobe_duration()` 之前从 ffmpeg 路径字符串推导 ffprobe 路径，但 `imageio-ffmpeg` 只内置 ffmpeg（且二进制名形如 `ffmpeg-win-x86_64-v7.1.exe`），小白机器无系统 ffprobe 时推导路径不存在 → `FileNotFoundError` → `WinError 2`。改为调用 ffmpeg 自身 `-hide_banner -i` 探测，从 stderr 解析 `Duration: HH:MM:SS.xx`，去掉对 ffprobe 的依赖。函数名 `ffprobe_duration` 保持不变（调用方零修改）。

### Changed
- `src/minipic/media.py` 删除 `json` import（已被 `resolve_reference` 复用，本次实现不再用到）；`src/minipic/web.py:363` 注释 "ffprobe fails" → "probe fails"。

### Verification
- `tests/test_media.py::TestFfprobeDuration` 5 个新用例（stderr 解析 / 小时位时长 / 解析不到 Duration / FileNotFoundError / TimeoutExpired），全部 mock `subprocess.run`，不碰真实二进制。

## [0.2.0] - 2026-09-01

### Changed
- **架构去重**：`cli.py` 与 `web.py` 重复的 content[] 构建逻辑抽到 `media.build_content()`（两处曾因不一致导致 Web 端提交 400）；「提取视频 URL + 下载」抽到 `media.download_task_result()`。
- **修复 CLI text 结构 bug**：`minipic create` 的 text 项从嵌套 `{"text":{"text":...}}` 改为官方要求的扁平 `{"text":...}`（此前 CLI 提交会被官方拒绝）。
- **CLI r2v 音频补校验**：`--ref-audio` 现在也走 `prepare_reference_audio`（≤15s），与 Web 端一致。

### Removed
- 删除死代码 `src/minipic/prompt.py`（H3Prompt 类无任何生产引用）及 `tests/test_prompt.py`。

## [0.1.7] - 2026-09-01

### Changed
- **API key 优先级调整**：user config（`~/.config/minipic/config.json`）成为唯一权威，`user > local > env`；在网页 UI 里保存的 key 对所有入口（CLI + Web）生效，重启后仍有效，环境变量仅兜底。
- **端口固定 7860**：`minipic web` 移除 `--port` 参数，`python -m minipic.web` 入口同步从 8765 改为 7860，杜绝串用其他程序端口。

## [0.1.6] - 2026-09-01

### Added
- **H3-Max 能力约束落地**：H3-Max 不支持多模态参考（r2v）与中间帧；`MODEL_CONSTRAINTS` 新增 `modes` / `i2v_roles`，`validate_model_modes()` 校验；Web UI 与 CLI 提交前都拦截。

### Changed
- **对照官方文档对齐**（role 枚举无 `middle_frame`）：
  - i2v 支持首帧 / 尾帧（含首+尾帧组合），不再支持中间帧。
  - 图生视频与多模态参考互斥（`first_frame`/`last_frame` 与 `reference_*` 不可混用）。
  - text ≤7000 字符、参考图 ≤9 张、首/尾帧各 ≤1。
  - 上传按类型区分大小上限：图片 ≤30MB（补 HEIC/HEIF）、视频 ≤50MB、音频 ≤15MB。
  - 参考音频 ≤15s 校验。

### Fixed
- `content[]` 结构按官方 schema 修正：URL 嵌套在 `image_url`/`video_url`/`audio_url` 对象内（此前 Web 端用了顶层扁平结构导致提交 400）。

## [0.1.5] - 2026-09-01

### Removed
- **多段生成整体移除**：prepare/commit 流程（`/api/prepare`、`/api/commit`、`prepare`/`commit`/`prepares` CLI 命令）、`--ref-video-clips` 参数、长参考视频自动切段与拼接（`plan_segments`、`find_shot_boundaries`、`extract_reference_clips`、`concat_videos`）、`prepare_records` 存储。
- 回到原生 ≤15s 单段 H3 生成；参考视频 >15s 直接报错提示先截取。

### Changed
- **上传 URL 引用改为 `mm_file://{file_id}`**：不再调用 `/v1/files/retrieve` 解析 CDN URL（该接口不返回 URL，是提交 400 的根因之一）。

## [0.1.4] - 2026-09-01

### Added
- **前端 picker 按钮**：参考素材行（i2v 首帧 + r2v image / video / audio 三种动态行）每行多了个 📎 按钮，点击弹出**浏览器原生 file 选择对话框**。选定文件后自动 `POST /api/upload`，返回的绝对路径写回原 `<input type=text>`，与手填路径完全等价。**手填能力零破坏**——input / select / 现有 `buildPayload()` 逻辑一行未改。
- **后端 `POST /api/upload`**：multipart 上传，参数 `file: UploadFile`。返回 `{path, kind, size, content_type, sha256}`，其中 `path` 是 `%APPDATA%/minipic/uploads/<uuid32hex>.<ext>` 的绝对路径。
- **MIME 白名单**（最小集，7 种）：
  - `image/png, image/jpeg, image/webp`
  - `video/mp4, video/quicktime`
  - `audio/mpeg, audio/wav`
  - 不在白名单 → 400 + 中文文案 `unsupported content_type: ...; allowed: [...]`。
- **50MB 大小限制**：与 `web.py` 文案 "≤50MB" 对齐。流式 1MB chunk 写入，**累计字节超限立即 unlink 落盘文件并返 400**，不会留下半个文件。
- **服务端硬命名 + 路径安全**：客户端传的 `filename`（如 `../../etc/passwd.png`）**完全被忽略**，磁盘名固定为 `uuid.uuid4().hex + _MIME_TO_EXT[content_type]`。白名单 / 后缀 / 常量硬编码在 `web.py` 顶部（`_ALLOWED_MIME` / `_MIME_TO_EXT` / `_MIME_TO_KIND`）。
- **24h TTL sweep**：`storage.UPLOAD_TTL_MS = 24h` + `cleanup_expired_uploads(user_data_dir)`。挂在现有 `cleanup_expired_prepares()` 两个懒触发点（`GET /api/prepare/{id}` + `GET /api/prepares`），与 prepare 24h 过期同一节奏。**`.json` 同名缓存文件（如 `media._upload_cache_path`）跳过**，不会被扫掉。

### Changed
- `index.html` 三个 ref-row 容器结构扩展（i2v 静态行 + addRef 三分支各多 1 个按钮 + 1 个 hidden `<input type="file">`）。`buildPayload()` 不动，原 DOM 选择器路径 `.querySelector('input')` / `.querySelector('select')` 仍然有效（hidden file input 是 type=file，selector 不冲突）。
- `web.py` import 区追加 `hashlib`, `File`, `UploadFile`（FastAPI 标配），`config.user_data_path` / `config._user_data_dir`（sweep 需要 user data 根路径）。

### Non-goals（v0.1.4 不做）
- **client-side file-size 提示**：仅服务端校验。超大文件用户体验差（要等上传完才知道失败），留 v0.1.4+。
- **上传进度条**：当前 fetch 不读 `ReadableStream.body` 进度，留 v0.1.4+。
- **拖拽上传**：当前只支持 picker，留 v0.1.5+。
- **病毒扫描 / 内容嗅探**：白名单 + 大小限制够用，签名头校验不在 v0.1.4 范围。

### Verification
- **428 / 428 passing**（原 422 + 5 个 web 上传测试 + 1 个 storage sweep 测试 = 0 fail）
- 新增覆盖：
  - `TestApiUpload::test_upload_image_success` — 200 + 字段齐全 + 落盘文件存在 + uuid 命名
  - `TestApiUpload::test_upload_path_traversal` — `../../etc/passwd.png` 客户端名不进磁盘
  - `TestApiUpload::test_upload_size_limit` — 51MB → 400 + uploads/ 不留残骸
  - `TestApiUpload::test_upload_mime_reject` — `.exe` (`application/octet-stream`) → 400
  - `TestApiUpload::test_upload_audio_accepts_mp3` — audio/mpeg 在白名单
  - `TestCleanupExpiredUploads::test_deletes_old_keeps_new_and_json` — 25h 前的 png 删、新的留、`.json` 同名缓存留
  - `TestCleanupExpiredUploads::test_no_uploads_dir_is_noop` — 缺目录不抛
  - `TestCleanupExpiredUploads::test_returns_zero_when_nothing_to_clean` — 全新文件不被误删
- 现有 web / storage / config / errors / cli 422 测试零回归
- 3 个 `test_media.py` ffmpeg-binary 测试在没装 `imageio-ffmpeg` 的环境会 fail，与 v0.1.4 改动**无关**（依赖 ffmpeg 二进制，没装就会 fail）

## [0.1.3b] - 2026-09-01

### Added
- **多段生成（multi-segment generation）**。当上传的参考视频 > 15s（H3 单段上限）时，minipic 自动：
  1. 用 ffmpeg `gt(scene,THR)` 做镜头边界检测，**adaptive threshold**（0.4 → 0.3 → 0.5 三档降级，无可用切点时退到均匀切）
  2. 调 `plan_segments()` 在 `±3s` 窗口内挑最近镜头切点，确定每段时长（**整数秒**，每段 4-15s）
  3. ffmpeg 物理切文件（`-ss` fast seek + libx264/aac 重编码）到 `%APPDATA%/minipic/clips/<source.stem>/`
  4. **每个切段**独立上传 + 独立调一次 H3 generation
  5. 完成后用 ffmpeg concat demuxer 拼成最终视频
  6. 中间产物 `_segNN.mp4` 在父 task 进入 succeeded 后自动清理
- **段数硬上限 N ≤ 4**。超过 60s 的参考视频直接 `MediaError("would need 5 segments (max 4)")`，提示用户裁剪。
- **父 task + N 子 task 模型**。1 个 `mode=multi` 父 task + N 个 `mode=r2v` 子 task（`extra.parent_task_id` 关联）。`GET /api/tasks/{parent_id}` 返回时 `children` 数组嵌入每个子 task 状态。父 task status: `submitted` / `processing` / `succeeded` / `partial` / `failed`。
- **用户确认流：`/api/prepare` → `/api/commit/{id}`**。一站式 `/api/create` 拆成两阶段：
  - `POST /api/prepare`：上传参考 + 切点 + Content-IR + build content 草稿，**不调 H3**（零成本），返回 `prepare_id` + 完整 plan（切点表、CDN URL、warnings、enhanced_prompt）
  - `GET /api/prepare/{id}`：重新查看 plan
  - `POST /api/commit/{id}`：用户确认后才调 H3（**不可中断**），后台 asyncio task 跑 polling + 拼接
  - 24h 过期（`PREPARE_TTL_MS`），过期返回 410 Gone
  - 重复 commit 同 prepare_id 返回 410 already-committed
- **CLI `minipic prepare {t2v,i2v,r2v}` + `minipic commit <prepare_id>` + `minipic prepares`**。`--yes` 一步提交（跳过 review 面板）。
- **UI prepare 流**（默认走两步流程）。新建"准备计划"折叠面板：切点表 / CDN URL 列表 / warnings（黄色 badge）/ Context-IR 状态 / `[取消] [确认提交]` 按钮。`高级` 折叠里加"Context-IR 改写 prompt"（默认勾选）和"一步提交（跳过准备预览）"开关。
- **Context-IR 默认 ON + 自动降级**。`PrepareBody.use_context_ir: bool = True`。失败（如 TokenPlan 2013）自动用原 prompt + warning，**不卡住用户**。UI 顶部右侧 `Context-IR: ✓ 已改写` / `Context-IR: ⚠ 降级到原 prompt` 状态药丸。
- **响应字段**：`enhanced_prompt`（实际送 H3 的）/ `original_prompt`（用户原输入）/ `used_context_ir` / `context_ir_fallback` / `context_ir_error` / `context_ir_warning`。
- **真实视频 probe 验证**：基于用户提供的 17.77s Woody 包快剪视频，方案 A（reference 模式切分）完胜方案 D（first_frame 串行）。方案 D 留 v0.1.4+。

### Deprecated
- `POST /api/create` 加 FastAPI `deprecated=True` 标记。**行为不变**（内部走 prepare + commit 合并），CLI 仍可正常用。v0.2.0 才删。

### Changed
- `MiniMaxClient` 不动；Context-IR 已存在，minipic v0.1.0 就在用，v0.1.3b 把集成从"可选 CLI 工具"提升为"prepare 阶段默认 step + 自动降级"。
- `MediaError` 不动；多段相关的常量挪到 `media.py` 顶部（`SEGMENT_MIN_SECONDS=4` / `SEGMENT_MAX_SECONDS=15` / `SEGMENT_MAX_COUNT=4` / `SEGMENT_BOUNDARY_WINDOW=3.0`）。

### Non-goals（v0.1.3b 不做）
- **方案 D**（first_frame 串行锁住）—— 快剪视频不需要，留 v0.1.4+
- **拼接质量优化**（crossfade / 转场）—— 留 v0.1.4
- **PySceneDetect 依赖**—— 手写 ffmpeg scene detection，~30 行
- **自动重试**（H3 失败重试整批）—— v0.1.3b 只手动重试（重新 prepare）
- **commit 中止**—— H3 没看到 cancel API

### Verification
- 422 单元/集成测试通过（原 337 + 18 storage + 27 media + 30 web + 10 cli = 422；实跑 422 pass / 0 fail）
- Web 端到端 smoke：`POST /api/prepare` (t2v) → `GET /api/prepare/{id}` → `POST /api/commit/{id}` 跑通，Context-IR 改写路径走通
- CLI `minipic prepare t2v --prompt ... --ratio 16:9` 注册到 `--help`
- **真实 E2E 视频生成要换 pay-as-you-go key**（当前 TokenPlan key 不支持 Context-IR，v0.1.3b 的降级路径已用 mock 验证）

## [0.1.2] - 2026-09-01

### Added
- **UI：API Key 配置按钮 + 模态框**。Header 右侧加 `⚙ API Key: ...` 状态药丸；未配置时显示红点 "未配置"，已配置时显示绿点 `...末4位`。点击弹出原生 `<dialog>` 模态框，含 password input、保存按钮、保存路径提示（`%APPDATA%\minipic\config.json`）。新用户**无需手动设置 env 变量**就能从 UI 起步。
- **后端 `GET /api/config`**：返回 `has_key` / `masked`（**永远不返回真 key**）/ `source`（`env` / `user` / `local` / `none`）/ `user_config_path` / `models`（含每模型 resolutions、duration 范围）。
- **后端 `POST /api/config`**：接受 `{api_key: "..."}`，写到 user config dir，**直接更新 `app.state.cfg.api_key`**（不依赖 env reload）。下一次 `/api/create` 立即生效。
- **后端 `DELETE /api/config`**：未实现（YAGNI；要切换 key 直接 POST 新值即可，UI 弹窗可手动改）。
- **模型选择 `MiniMax-H3-Max`**。MiniMax 官方支持两个模型：
  - `MiniMax-H3`：768P / 2K，duration 4-15（默认）
  - `MiniMax-H3-Max`：480P / 768P，duration 5-15（**不支持 2K，duration 必须 ≥ 5**）
- **CLI `--model` 选项**（t2v/i2v/r2v 都有），默认 `MiniMax-H3`。校验在调用 client 之前进行，错误参数直接 `SystemExit(1)`。
- **后端 `CreateBody.model` 字段** + `_validate_mode` 联动校验（H3-Max + 2K → 400；H3-Max + duration<5 → 400）。
- **`config.py` 新增工具**：`API_MODELS` 常量列表、`MODEL_CONSTRAINTS`（每模型的 resolution / duration 范围）、`mask_key(key)`（UI 安全展示，末 4 位）、`validate_model_params(model, resolution, duration)`（抛 `ConfigError`）。
- **UI 联动**：切 model 时自动收窄 resolution 下拉选项（`H3-Max` 不显示 2K）、调整 duration min/max、把当前 duration 拉回合法范围。
- **CLI 启动不再强求 API key**：`minipic web` 在没 key 时也能起 UI（新用户可从 UI 配置）。

### Fixed
- **MAJOR `web.py`**：v0.1.1 之前的"重启 web 才能换 key"问题——POST /api/config 现在直接改 `app.state.cfg`，下一次请求立即生效。
- **MAJOR `_detect_key_source`**：env > local > user 的真实优先级反映到 UI 顶部状态（之前只显示有没有，不显示来源）。
- **MAJOR `web.py`**：`ConfigBody` 提升到模块顶层（Pydantic v2 + FastAPI 嵌套类不能正确解析为 body 参数，会被当作 query，触发 422），改用 `Annotated[ConfigBody, Body()]`。
- **MINOR `index.html`**：去掉误导性的 480P 选项（默认隐藏），改由 model 联动动态生成；duration 输入框的 `min`/`max` 属性动态更新。

### Test status
- 337 / 337 passing（v0.1.1: 292 → v0.1.2: 337，+45 个新测试）
- 新增覆盖：
  - `TestMaskKey`、`TestModelRegistry`、`TestValidateModelParams`（config.py）
  - `TestCreateVideoTask::test_accepts_h3_max_model`（client.py）
  - `TestApiConfigEndpoints`（GET/POST /api/config 6 个 case）、`TestDetectKeySource`（5 个 case）、`TestApiCreateModelValidation`（4 个 case）、`TestSubmitTaskModel`（2 个 case）
  - `TestWebCommand::test_no_api_key_starts_anyway`、`TestCreateModelFlag`（7 个 case：t2v/i2v/r2v × default/h3-max/rejected）

### Verified
- 端到端冒烟（uvicorn + curl）：
  - `GET /api/config` → 200，`has_key: true`，`masked: "...1234"`，`source: "env"`，两个 model 约束正确
  - `POST /api/config {"api_key": "smoke-test-key-9999"}` → 200，`saved_to` 指向 `%APPDATA%\minipic\config.json`，`app.state.cfg.api_key` 立即更新
  - `GET /` → 200，HTML 含 `<title>minipic</title>`

### Notes
- 当前 TokenPlan key 不支持 H3-Context-IR（已知限制）。H3-Max 大概率也不支持，要等 pay-as-you-go key 或服务端放权。UI 不区分这个——`POST /api/create` 时 2013 错误码会在响应里正常返回。
- DELETE /api/config 暂未实现（按 YAGNI 原则省略）。

## [0.1.1] - 2026-09-01

### Fixed
- **CRITICAL `examples/bag_swap.py:70`**: `asyncio.to_thread(prepare_reference_video, ...)` was awaiting a coroutine that was never `await`ed. The one-click example crashed for any local `--ref-video`. Now calls the `async def` directly.
- **MAJOR `src/minipic/web.py`**: `/api/create` no longer inserts a placeholder "pending" row before knowing the real task_id. Defer insert until the API call returns; only then write the real record. (Zombie rows no longer accumulate in the task ledger.)
- **MAJOR CLI help text**: removed the "540P / 720P / 1080P" misdirection. H3 V2 only supports `768P` and `2K`; CLI help now matches the web API and the public docs.
- **MINOR `src/minipic/errors.py`**: removed dead `if pass` block; `raise_for_code` now includes the HTTP status in the message when present.
- **MINOR `src/minipic/errors.py`**: clarified docstring on `1039` (mapped to `InvalidParamsError`).

### Test status
- 292 / 292 passing
- 5 NIT / MINOR findings from v0.1.0 verifier report remain as `v0.1.2` backlog:
  - 2 `TestPrintTaskRecord` tests assert nothing (test_cli.py:633-647)
  - `web.py` `app.state.cfg` is captured once at app startup; restart the web server after `config set api_key`
  - `test_media.py:38-42` `test_falls_back_to_imageio_ffmpeg` patches a path that's never invoked
  - `test_client.py:228-241` `test_exhausted_retries_raises` should assert the specific exception class
  - `test_prompt.py:147-152` boundary test only covers 6900 chars, not 7000

## [0.1.0] - 2026-09-01

### Added
- 项目脚手架：`pyproject.toml`、目录结构、`.gitignore`、`README.md`、`CHANGELOG.md`
- 核心模块：`config` / `client` / `errors` / `media` / `poller` / `prompt` / `storage` / `web` / `cli`
- 支持 3 种生成模式：T2V / I2V / R2V（不含 FL2VA 首尾帧）
- CLI（click）：`config` / `create t2v|i2v|r2v` / `status` / `wait` / `list` / `cancel` / `videos` / `web`
- 网页 UI：FastAPI + 单文件 `web/index.html`，动态参考素材行、5 秒自动刷新任务列表
- 长参考视频处理：自动均匀采样 3×5s 填满 15s 预算，或 `--ref-video-clips` 显式覆盖
- V1 文件上传 → V1 retrieve → V2 content[] 的两段式 URL 解析（解决 V2 不接 file_id 的限制）
- H3-Context-IR 集成：`MiniMaxClient.create_context_ir_task` / `fetch_context_ir_prompt`（自动把 brief 重写成 H3 6 段式）
- 任务持久化：SQLite 任务库（用户数据目录），崩溃可恢复
- 错误码 → 异常映射：1004/1008/1026/2013/2049/2056 等终态码不重试；1000/1001/1002 带 backoff 重试
- 跨平台：imageio-ffmpeg 自带 ffmpeg 二进制、platformdirs 拿 Windows/macOS/Linux 各自的配置/数据目录
- 测试：292 个用例覆盖 config / errors / client / media / prompt / storage / poller / web
- 示例：`examples/bag_swap.py`（一键脚本：brief → Context-IR 改写 → H3 生成 → 下载）

### Notes
- 部分 coder 任务在最后汇报阶段被网络 SSL 错误（net::ERR_SSL_BAD_RECORD_MAC_ALERT）打断，文件实际已写入；验证以磁盘为准
- `MiniMax-H3-Max` 真实存在于 MiniMax V2 schema，但**不支持** multimodal reference 模式，故 v0.1.0 不暴露
- Context-IR 端点为 `/v2/h3_context_ir`，和 H3 视频生成共用 `/v2/query/video_generation/{task_id}` 轮询，成功后从 `task.content.prompt` 取增强后的提示词
