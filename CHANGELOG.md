# Changelog

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
