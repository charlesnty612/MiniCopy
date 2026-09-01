# Changelog

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
