# Changelog

## [0.1.1] - 2026-09-01

### Added
- `tests/test_web.py` — 50 个用例覆盖 FastAPI app factory、`/api/create` (t2v/i2v/r2v 模式校验 + 错误路径 + wait=True 下载)、`/api/tasks` 列表/详情/刷新、`/api/tasks/{id}/download`、CreateBody pydantic 模型、`_validate_mode` / `_submit_task` / `_record_to_dict` 内部 helper
- `tests/test_cli.py` — 50 个用例覆盖 Click `main` 子命令、`config show/set` (含 type coercion 和 scope)、`create t2v|i2v|r2v`、`status` / `wait` / `list` / `cancel` / `videos` / `web` 命令、`_status_label` / `_parse_ref` / `_print_task_record` / `_wait_and_download` / `_atomic_download` helper

### Fixed
- `web.py`: `wait=True` 流程里 `cfg.videos_dir / f"{task_id}.mp4"` 崩溃（videos_dir 是 string），改为 `Path(cfg.videos_dir) / f"{task_id}.mp4"`
- `cli.py`: `_print_json` 把 dict 序列化成 JSON string 再传给 `rich.print_json`，导致输出被双引号包起来；改为直接传 dict 给 rich
- `cli.py`: `_parse_ref` 在 URL (`https://...`) 和 Windows 路径 (`C:\...`) 上会错误地把第一个 `:` 当作 role 分隔符；改为先识别 URL/Windows 路径，只在尾部匹配 `:role` 形式
- `storage.py`: `_conn()` 不会确保表存在，导致全新 DB 文件上第一次 `update_task()` 报 `no such table: tasks`；改为 inline `CREATE TABLE IF NOT EXISTS`（不能用 `init_db()`，会和自身递归）

### Test status
- 292 个测试全过（v0.1.0 时 240/212；本版本净增 100 个 web/cli 用例，剩余 9 个旧失败由本批次之外的 sibling task 修复）

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
- 测试：292 个用例覆盖 config / errors / client / media / prompt / storage / poller / web，全过
- 示例：`examples/bag_swap.py`（一键脚本：brief → Context-IR 改写 → H3 生成 → 下载）

### Known limitations
- 本地文件先走 V1 上传再 retrieve 拿 CDN URL；如果 V1 retrieve 不返回 URL，需要改用 `--ref-image-url` 远程 URL
- `cancel` 子命令目前只打印提示（V2 没有公开的 cancel API，得在 platform.minimaxi.com 后台手动取消）
- H3 V2 不支持 MiniMax-H3-Max 的 reference-to-video 模式

### Notes
- 部分 coder 任务在最后汇报阶段被网络 SSL 错误（net::ERR_SSL_BAD_RECORD_MAC_ALERT）打断，文件实际已写入；验证以磁盘为准
- `MiniMax-H3-Max` 真实存在于 MiniMax V2 schema，但**不支持** multimodal reference 模式，故 v0.1.0 不暴露
- Context-IR 端点为 `/v2/h3_context_ir`，和 H3 视频生成共用 `/v2/query/video_generation/{task_id}` 轮询，成功后从 `task.content.prompt` 取增强后的提示词
