# MiniCopy 微信小程序实施方案

> 版本：v1.1（2026-09-03） · 主会话方案设计产出，作为后续派发的唯一依据
>
> 已确认决策：**个人主体**（用户已用个人身份注册小程序）✅ · 用户自填 MiniMax key（平台不承担费用）✅ · 后端复用 FastAPI 改造 ✅
>
> v1.1 变更：主体由企业改为个人。对技术架构无影响（个人小程序同样支持 wx.login/openid、内容安全 API、服务器域名配置）；主要影响在**类目审核风险**，见第 5 节。

---

## 0. 现状与改造总原则

现有架构：FastAPI 单进程（`src/minipic/web.py`，8 个 `/api/*` 端点）+ 单 HTML 前端（`web/index.html`）+ SQLite（`storage.py`），单机零鉴权，端口 7860。

**总原则**：
1. **业务核心复用**：`client.py`（MiniMax 调用）、`poller.py`（状态机）、`web.py:526-682`（模式/模型/参数校验）、上传处理逻辑全部保留。
2. **Web 层加用户体系**：新增微信登录鉴权，所有业务接口按 `user_id` 隔离。
3. **前端整体重写**：原生小程序（不引 Taro/uni-app），单页面应用拆为 3 个页面。
4. **CLI 与现有 Web UI 不受影响**：新代码放新模块，`web.py` 走兼容式改造，本机单机模式继续可用。
5. **严禁真实扣费调用**：开发自测一律 mock MiniMax API；仅最终人工验收时可由用户本人决定是否实测。

---

## 1. 目标架构

```
微信小程序（原生，3 页面）
   │  HTTPS + Authorization: Bearer <token>
   ▼
云服务器：FastAPI（7860 → Caddy/Nginx 反代 HTTPS）
   ├── auth.py     微信登录、token 签发/校验（新增）
   ├── store 层    users 表 + tasks 表加 user_id（改造）
   ├── web.py      现有 8 端点加鉴权与用户隔离（改造）
   └── client/poller/media/校验逻辑（复用，不改）
   │
   ▼
MiniMax API（每个请求用该用户自己存的 key）
```

部署：Linux 服务器 + systemd 长驻 + Caddy 自动 HTTPS，域名需已 ICP 备案并配置进小程序后台"合法域名"。

---

## 2. 后端改造清单（按依赖排序）

### B1. 用户体系与微信登录（新增 `src/minipic/auth.py`）
- `POST /api/auth/login`：入参 `{code}`（小程序 `wx.login` 得来）→ 调微信 `code2session`（AppID/AppSecret 存服务端环境变量）→ 拿 `openid` → users 表查/建 → 签发 token（随机 32 字节 hex，存 SQLite `sessions` 表，30 天过期）→ 返回 `{token}`。
- 鉴权依赖注入：`require_user(request) -> user_id`，校验 `Authorization: Bearer <token>`，失败 401。B2 起所有 `/api/*`（除 login 与 `/` 静态）都挂它。
- AppID/AppSecret 走环境变量 `MINIPIC_WX_APPID` / `MINIPIC_WX_SECRET`，不进配置文件、不进仓库。
- **验收**：mock 微信 code2session 的单测通过；无 token 访问 `/api/tasks` 返回 401。

### B2. 存储层改造（`storage.py`）
- 新增 `users` 表：`user_id INTEGER PK, openid UNIQUE, api_key TEXT, created_at`。
- `tasks` 表加 `user_id` 列（含迁移逻辑：老库 ALTER TABLE，存量行归 user_id=0 即本机模式）。
- uploads/videos 目录按用户隔离：`uploads/{user_id}/`、`videos/{user_id}/`。
- API key 按用户存取：`get_api_key(user_id)` / `set_api_key(user_id, key)`；**优先级改为：用户库 key > env**（本机无登录场景保持现状）。
- **验收**：迁移脚本对老 `tasks.db` 无损；两个用户的任务/文件互不可见的单测。

### B3. 现有端点用户隔离（`web.py`）
- `/api/config` GET/POST 改为读写**当前用户**的 key（不再是全局文件）；响应结构不变。
- `/api/create`：用当前用户的 key 构造 client；无 key 返回 422 `{error:"no_api_key"}`。
- `/api/tasks`、`/api/tasks/{id}`、DELETE：只查/删当前用户的行。
- `/api/upload`：落到 `uploads/{user_id}/`，其余逻辑（大小帽/MIME 白名单/UUID 命名）不动。
- **验收**：双用户隔离单测；无 key 提交返回 422。

### B4. 视频播放与下载（`web.py`）
- 新增 `GET /api/tasks/{id}/play`：返回 `FileResponse(media_type="video/mp4")` **不带** `Content-Disposition: attachment`，支持 Range（FastAPI FileResponse 默认支持），供小程序 `<video>` 在线播放；校验任务归属当前用户。
- 原 `/download` 保留（附件下载，供 Web UI 与"保存到相册"流程用 `wx.downloadFile`）。
- **验收**：play 端点 Range 请求返回 206；跨用户访问返回 404。

### B5. 部署服务化
- `web.py` 启动 host 支持 `--host 0.0.0.0`（已有），端口保持 7860；新增 `scripts/deploy/systemd unit` + Caddyfile 示例 + `docs/DEPLOY_SERVER.md`（服务器部署：Python 环境、systemd、Caddy 反代、HTTPS、防火墙）。
- CORS：小程序 `wx.request` 不强制 CORS，**不开放 `*`**；保留 localhost 白名单供 Web UI 同源使用即可。
- **验收**：文档按步可在干净 Linux 机器复现（人工验收）。

### B6. 合规接入（后端部分）
- 提交 `/api/create` 前，对 `prompt` 调微信 `security.msgSecCheck`（服务端 API，需 access_token 缓存）；命中违规返回 422 `{error:"content_rejected"}`。
- i2v/r2v 上传图片调 `security.imgSecCheck`（或 mediaCheckAsync 异步）。
- **验收**：mock 违规文本被拦截的单测。

---

## 3. 小程序前端（原生，新目录 `miniprogram/`）

### 页面结构（3 页 + 1 组件）
| 页面 | 路径 | 对应现有功能 |
|---|---|---|
| 生成页 | `pages/create/create` | `index.html` 整个左列：模式切换、模型/提示词/时长/比例/分辨率、i2v 首尾帧、r2v 动态 ref 行、wait/context-ir 开关、防连点提交 |
| 任务页 | `pages/tasks/tasks` | 任务列表（10s 轮询、中文状态、呼吸动画用 CSS animation 实现）+ 详情面板（内联展开，不做 dialog） |
| 我的 | `pages/me/me` | Key 配置（填/改/查看掩码）、版本号、用户协议/隐私政策入口 |

### 关键技术口径（派工时给死）
- 登录：app.js `onLaunch` → `wx.login` → `/api/auth/login` → token 存 `wx.setStorageSync('token')`；`wx.request` 封装统一加 header，401 时静默重新登录一次再重试。
- 上传：`wx.chooseMedia`（图片/视频）/ `wx.chooseMessageFile`（音频）→ `wx.uploadFile` 到 `/api/upload`（带 token）→ 拿回 path 存 data。临时路径不跨页面传。
- 动态 ref 行：`wx:for` 渲染数组，add/remove 走 `setData` push/splice；模式/模型/时长的联动校验全部从 `index.html:1158-1201` `applyModelConstraints()` 翻译为 JS 纯函数，禁用项灰显。
- 播放/保存：详情里 `<video src="{{playUrl}}" binderror>`；保存相册用 `wx.downloadFile` → `wx.saveVideoToPhotosAlbum`（需 `scope.writePhotosAlbum` 授权，拒绝时 `wx.showModal` 引导去设置页）。
- 合规展示：生成结果下方固定"内容由 AI 生成"标识；首次进入弹隐私政策授权。
- 域名：request/uploadFile/downloadFile 合法域名统一 `https://<备案域名>`。

---

## 4. 派发计划（接口契约定稿后 B 与 F 可并行）

| # | 任务 | 类型 | 依赖 |
|---|---|---|---|
| 1 | B1+B2 用户体系与存储改造 | general 开发 | — |
| 2 | B3+B4 端点隔离与播放 | general 开发 | 1 |
| 3 | B5 部署文档与脚本 | general 开发 | 2 |
| 4 | B6 内容安全接入 | general 开发 | 2（需小程序 AppID） |
| 5 | F1 小程序骨架+登录+我的页 | general 开发 | 1 的契约 |
| 6 | F2 生成页（含上传） | general 开发 | 5、3 |
| 7 | F3 任务页+播放/保存 | general 开发 | 5、3 |
| 8 | F4 合规页面（协议/隐私/AI标识） | general 开发 | 5 |
| 9 | 每阶段完成后审查 | general 只读审查 | 各开发任务 |

> 编码类任务 1→2→3 串行（同改 web.py/storage.py）；F 系列与 B 系列契约定稿后并行；F 内部 5 先行，6/7/8 并行。

## 5. 风险与未决项
- **类目审核风险（个人主体的主要影响）**：个人小程序可选择的类目有限，"AI 生成/深度合成"相关类目通常要求企业资质。可行路径：以「工具 > 图片/视频处理」类个人可申类目提交，页面与描述如实说明功能但不夸大；**仍有被驳回并要求升级企业主体的可能**，届时需用户本人补办主体升级（小程序支持个人主体升级为企业）。此风险必须在开工前知悉并接受。
- **个人主体的其他限制**：无法使用微信支付（本项目用户自填 key，无支付需求，无影响）；无法做微信认证（不影响 wx.login/openid、内容安全 API 的使用）；服务器域名要求与企业主体相同（HTTPS + ICP 备案，个人名义可备案）。
- **内容安全额度**：`msgSecCheck` 免费但有限频，量大了要评估。
- **微信登录必须有 AppID**：B1/B6 开发依赖小程序 AppID（个人主体已注册即有 AppID，可直接开工；未发布前用开发版/体验版自测）。
- **视频体积**：H3 生成的 mp4 可能几十 MB，`wx.downloadFile` 单文件上限 50MB，超长任务建议优先引导在线播放。
