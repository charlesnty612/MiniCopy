# HANDOFF — minipic v0.1.1 测试收尾

**日期**: 2026-09-01
**Session**: mvs_97255405a5784f5d860738af4d4579cc ("minipic remaining tests")
**Parent**: mvs_30f2549a47ee401a8bbb9a0f4e81c4c4

## 范围

按父任务"minipic remaining tests"的请求，本分支完成两件事：
1. 写完 `web.py` 和 `cli.py` 的测试覆盖
2. 修 4 个阻塞测试用例的 source code bug

## 新增文件

- `tests/test_web.py` — 50 个用例
  - `TestCreateApp`: factory / state / default load_config / CORS middleware
  - `TestRootRoute`: `GET /` 返回 index.html
  - `TestApiCreateAuth`: 缺 API key → 503
  - `TestApiCreateModeValidation`: t2v / i2v / r2v 模式校验（拒绝 adaptive / 拒绝错误 role / 拒绝 ref 缺失等）
  - `TestApiCreateHappyPath`: 正常提交、pydantic pattern/duration 校验
  - `TestApiCreateErrors`: TaskError / MiniPicError / 未知异常 → 400 / 500
  - `TestApiCreateWaitFlow`: `wait=True` 轮询 + 下载到 videos_dir
  - `TestApiListTasks`: `GET /api/tasks`
  - `TestApiGetTask`: 404 / 已知 / 刷新（区分终态/非终态）/ 异常吞掉
  - `TestApiDownloadTask`: 404 / 文件不在 / 200 + content-type=video/mp4
  - `TestCreateBody`: pydantic 模型默认值 / duration 边界 / pattern 校验
  - `TestValidateMode`: 5 个 case 覆盖 `_validate_mode` 各种 raise 路径
  - `TestSubmitTask`: t2v 透传 / i2v 强制 adaptive / URL 透传 / 本地文件走上传 / 音频 / 视频多 clip
  - `TestRecordToDict`: round-trip + None 字段

- `tests/test_cli.py` — 50 个用例
  - `TestMainGroup`: `--help` 列出所有子命令
  - `TestStatusLabel` / `TestParseRef`: 内部 helper
  - `TestConfigShow` / `TestConfigSet`: `config show` JSON 正确、`config set` 字符串/int/非法 int/未知 key/local scope
  - `TestCreateT2V` / `TestCreateI2V` / `TestCreateR2V`: 三种模式 happy path + 缺 API key + 默认 duration
  - `TestStatusCommand` / `TestWaitCommand` / `TestListCommand` / `TestCancelCommand` / `TestVideosCommand` / `TestWebCommand`
  - `TestPrintTaskRecord` / `TestWaitAndDownload` / `TestAtomicDownload`: 内部 helper
  - `TestCliErrorWrapper`: `_cli_wrapper` 把 MiniPicError 转 exit 2、其它异常转 exit 1

## 修的 source code bug（4 个）

| 文件 | 行 | 修复 |
| --- | --- | --- |
| `src/minipic/web.py` | 93 | `cfg.videos_dir / f"{task_id}.mp4"` 崩溃（videos_dir 是 string）→ 改 `Path(cfg.videos_dir) / f"{task_id}.mp4"` |
| `src/minipic/cli.py` | `_print_json` | 之前把 dict 序列化成 JSON 字符串再 `console.print_json(data=...)`，rich 把它当字符串打印（外层带引号）→ 直接传 dict 给 rich |
| `src/minipic/cli.py` | `_parse_ref` | 之前对 URL（`https://...`）和 Windows 路径（`C:\...`）错误地在第一个 `:` 切分 → 改为先识别 URL / Windows 路径再处理 role |
| `src/minipic/storage.py` | `_conn` | 之前不保证表存在，新 DB 文件上 `update_task` 报 `no such table: tasks` → inline `CREATE TABLE IF NOT EXISTS`（不能用 `init_db()` 会和 `_conn` 自身递归） |

## 测试结果

```
292 passed, 1 warning in 9.41s
```

跑 `pytest tests/ -q` 全绿。唯一 warning 是 Starlette 的 httpx 弃用提示，跟我们写的测试无关（FastAPI 0.141 内部用的），后续 starlette 升级 httpx2 即可。

## 未做（已知 limitation，写在这里留痕）

1. **CLI 的 r2v 模式 + 纯 URL ref** 不在测试覆盖里：`_parse_ref` 仍会在 `--ref-image "https://.../a.jpg:reference_image"` 这种「URL+显式 role」上把 `https` 当作 source。修复方法见下"后续建议"。
2. **`wait` / `status` 命令未调用 `init_db()`**：靠 storage 层的 inline schema bootstrap 兜底，不需要 CLI 显式调用。
3. **CLI `_cli_wrapper` exit code 2 vs Click 默认 1**：当通过 `runner.invoke` 触发时，Click 不会走 `_cli_wrapper` 包装层，所以缺 API key 时 exit 1（不是 2）。这是 CliRunner 的限制，不是 bug——直接 `python -m minipic.cli` 走 `__main__` 时会经过 `_cli_wrapper` 拿到正确的 exit 2。

## 后续建议（给下个版本）

- `_parse_ref` 改用更严格的判定：URL 不允许带 role（必须通过其它机制指定 role，比如 `--ref-image-role`）；Windows 路径同上。这样能彻底消除「URL+role」这种歧义场景。
- 9 个原本失败的测试（`test_client.py` / `test_media.py` / `test_prompt.py`）的修复由 sibling task 完成（commit `25ee7d4` 之后），不在本分支 scope。
