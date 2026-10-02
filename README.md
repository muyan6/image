> 当前版本使用云端异步链路。网页支持独立注册登录，注册初始 0 光子，仅由后台手动增加；匿名浏览不创建用户。微信绑定入口已准备，未配置时仍可独立使用网页。网站与跨端共享账户接入见 [`backend/WEB_SETUP.md`](backend/WEB_SETUP.md)，云端配置见 `backend/PLATFORM_SETUP.md`。历史本机/降级说明仅用于旧版本排障。

# 废片新生所 (Photo Rescue)

简约高端海报报刊风（Editorial Art & Gallery Poster）修图小程序 + 异步 AI 增强后端 + 网页管理后台。

小程序采用 4 大标签页策展架构（创作 · 模板 · 社区 · 我的），兼备极简优雅艺术感与完整商业化闭环。

| 档位 | 默认引擎 | 输出 | 成本（估算） |
|---|---|---|---|
| `light` | 中转网关 `gpt-image-2.5`（提示词驱动修图） | 长边 1536 净化增强 | ≈ ¥0.04 / 张 |
| `fine` | 中转网关 `gemini-3.1-flash-image-preview` | 生成式细节重构 | ≈ ¥0.15 / 张 |

引擎链路、模型、密钥、价格、提示词全部在**网页后台 `/admin` 热调**，改完即生效。
密钥未配置时**自动降级**：`中转网关 → fal.ai → 百度 AI → 本地 OpenCV 引擎`，
最后一级永远可用。

---

## 一、快速启动

### 1. 后端

服务器上**双击 `run_backend.bat`** 即可，无需任何手动步骤。脚本会自动：

1. 找 Python（优先项目内 `.venv`，没有就 `py -3` / `python`）；
2. 首次运行：创建虚拟环境 → 装依赖（国内镜像优先、官方源兜底）→ 复制 `.env.example` 为 `.env`；
3. 起服务（`0.0.0.0:8000`），**内置守护：进程崩溃 3 秒后自动拉起**，
   连续快速崩溃 5 次才放弃并提示查日志；
4. 日志同时写到 `backend/logs/backend_日期.log`（UTF-8，可直接 grep/tail）。

停止服务：**双击 `stop_backend.bat`**（先结束守护窗口再释放端口，防止自动拉起），
或命令行 `stop_backend.bat 8000`；也可在启动窗口按 Ctrl+C 后按 Y。

端口/地址在 `run_backend.bat` 顶部的 `HOST` / `PORT` 两个变量里改。

> 开机自启（可选）：`schtasks /create /tn PhotoRescue /tr "F:\Project\image\run_backend.bat" /sc onstart /ru SYSTEM`
> 需要管理员权限运行一次；删除用 `/delete /tn PhotoRescue /f`。

启动后：

- 服务地址 <http://127.0.0.1:8000>
- **管理后台 <http://127.0.0.1:8000/admin>**（密码 = `.env` 的 `ADMIN_PASSWORD`，首次启动自动生成并打印在日志里）
- 接口文档 <http://127.0.0.1:8000/docs>
- 健康检查 <http://127.0.0.1:8000/api/health>

> 注意：任务持久化在 SQLite，调度与索引仍为单进程，**不要**给 uvicorn 加 `--workers` 多进程，
> 多实例部署前先看「已知限制」。

### 2. 配置密钥（在网页后台做）

打开 `/admin` 登录后，在「供应商与提示词」里填中转网关的 API Key
（fal / 百度密钥也会在首次启动时从 `.env` 自动迁移进来）。改动即时生效，不用重启。

### 2.5 内容审核（分级省钱）

机审开启后按图片大小自动分级：

- **≤10M** → 微信官方 `mediaCheckAsync`（免费）。需要在微信公众平台
  「开发 → 开发设置 → 消息推送」配置：URL = `https://image.myil.top/api/wxpush`，
  Token = 后台「内容审核」里的推送 Token，**消息加密方式选「明文模式」、数据格式选「JSON」**，
  并把 `image.myil.top` 加进 request 合法域名（需 ICP 备案）。微信审核结果 1~3 秒推送回来，
  提交请求同步等待（上限 12 秒），超时按「异常拦截」策略处理。
- **>10M 或微信链路不可用** → 腾讯云 IMS（≈ ¥0.0015/张，走已配密钥）；
- 两级都不可用 → 放行（或按「异常拦截」开关处理）。

**域名规划（本项目实际使用）**：

| 域名 | 用途 | 小程序白名单 |
|---|---|---|
| `image.myil.top` | 后端 API（已写入 `app.js` 的 `apiBase`） | request / uploadFile / downloadFile 都加 |
| `photo.myil.top` | COS 自定义源站域名（后台「腾讯云 → 自定义域名」填它） | request + downloadFile |

COS 建议在后台「腾讯云」卡片里填**自定义源站域名**：配了之后所有签名直链都走它，
小程序只需在「服务器域名」白名单里放这一个域名；不填则用默认桶域名
（`*.myqcloud.com` 未备案，加不进小程序白名单）。

### 3. 小程序

1. 打开微信开发者工具 → 导入项目
2. 目录选 `miniprogram/`
3. AppID 选「测试号」
4. 真机调试时把 `miniprogram/app.js` 里的 `apiBase` 改成局域网 IP 或已备案域名 —— `127.0.0.1` 在手机上指向手机自己

### 4. 云服务器部署（Linux + Gitee）

本仓库的部署形态：Windows 侧只做开发（用上面的 `.bat`），**Linux 云服务器上用 shell 脚本**。

**首次部署（在服务器上跑一次）：**

```bash
git clone <你的Gitee仓库地址> photo-rescue && cd photo-rescue
bash deploy/install-service.sh        # 注册 systemd：开机自启 + 崩溃 3 秒自动拉起
chmod +x .update                      # 首次给更新脚本执行权限（只需一次）
```

脚本会自动创建 venv、装依赖（清华源）、补 OpenCV 需要的系统库（libGL）、
写入 systemd 服务并启动。看日志：`journalctl -u photo-rescue -f`。

**以后每次更新（拉取 Gitee 最新代码）：**

```bash
./.update
```

一条命令自动完成：`git pull` → requirements 有变化才重装依赖 → 重启服务
（优先 systemd，未注册时自动退回 `start.sh`/`stop.sh`）→ 健康检查，
失败会直接打印最近 30 行日志。没有新提交时秒级结束，不会瞎重启。

说明：

- 运行数据（`backend/data/`、`.env`、`uploads/`、`logs/`）都在 `.gitignore` 里，
  服务器上 `git pull` 不会和运行状态冲突；换服务器时这些文件手动迁移；
- 云服务器安全组需放行后端端口；小程序正式上线需要 **ICP 备案域名 + nginx 反代**（见「已知限制」）；
- 无 systemd 的环境（如容器）直接用 `bash start.sh [端口]` / `bash stop.sh [端口]`，
  但没有崩溃自动拉起，建议尽量走 systemd。

---

## 二、架构

```
小程序 / 网页
    │  POST /api/rescue          提交，立刻返回 job_id
    │  GET  /api/jobs/{id}       轮询状态
    │  GET  /api/images/{name}   取图
    │  GET  /api/config          价格/维护状态/风格表
    │  GET  /api/announcements   公告横幅数据
    ▼
FastAPI + 线程池（异步任务，不阻塞事件循环）
    │
    ├─ 归一化：长边 1536（后台可调），JPEG q95
    ├─ AI 增强：按 settings.chain 依次尝试
    │     中转网关 → fal.ai → 百度 AI → 本地 engine.py（永远兜底）
    └─ /admin 管理后台：配置热调 + 公告 + 任务记录
            └── backend/data/settings.json（供应商/密钥/模型/价格/提示词）
                backend/data/announcements.json（公告）
```

**为什么是异步的？** 模型推理要 15~30 秒，同步请求会撑爆小程序 `wx.request` 的超时。提交后立刻拿 `job_id`，前端轮询，用户关掉页面任务也继续跑。

### 文件职责

| 文件 | 作用 |
|---|---|
| `backend/main.py` | FastAPI 路由、任务队列、动态降级链、安全校验 |
| `backend/settings_store.py` | 运行时设置 + 公告存储（原子写、损坏自愈） |
| `backend/templates_store.py` | 模板与分组存储（后台热调，见「模板系统」） |
| `backend/text_overlay.py` | 中文文字排版合成（海报/明信片：AI 底图 + 代码叠字） |
| `backend/gateway_ai.py` | OpenAI Images 兼容网关客户端（worldcodes/Sub2API 类） |
| `backend/fal_ai.py` | fal.ai 双档客户端（队列提交 + 轮询 + 重试） |
| `backend/baidu_ai.py` | 百度 AI 备选客户端 |
| `backend/engine.py` | 本地 OpenCV 引擎（自适应诊断 + 风格调色） |
| `backend/admin_api.py` | 后台 API（登录/设置/公告/模板/任务），cookie 鉴权 |
| `backend/admin.html` | 管理后台单页（原生 JS，无构建） |
| `backend/tools/gen_covers.py` | 占位封面生成脚本 |
| `miniprogram/utils/api.js` | 小程序接口封装（上传 + 轮询 + 错误分类） |
| `miniprogram/pages/index/` | 首页：公告横幅、选图、模板工坊、提交 |
| `miniprogram/pages/compare/` | 对比页：拖拽滑块、保存相册 |

---

## 三、接口

### `POST /api/rescue`

| 字段 | 类型 | 说明 |
|---|---|---|
| `image` | file | JPG / PNG / WebP / BMP / TIFF，≤ 25 MB，≤ 6000 万像素 |
| `quality` | form | `light` 或 `fine` |
| `style` | form | 可选，`fuji` / `clear` / `gym_contrast`；留空按档位取默认 |

维护模式开启时返回 `503`，`detail` 为后台设置的维护文案。

立刻返回：

```json
{"code": 0, "job_id": "a1b2c3d4e5f6", "status": "processing",
 "orig_url": "/api/images/orig_....jpg",
 "result_url": "/api/images/result_....jpg"}
```

### `GET /api/jobs/{job_id}`

```json
{"id": "a1b2c3d4e5f6", "status": "succeeded", "stage": "done",
 "quality": "fine", "provider": "worldcodes", "error": null,
 "orig_url": "...", "result_url": "...", "created_at": 1790522908.88}
```

`status`：`processing` / `succeeded` / `failed`
`stage`：`queued` → `normalize` → `enhance` → `finalize` → `done`
`provider`：实际完成修图的供应商（`worldcodes` / `fal` / `baidu` / `local`）

### `GET /api/config`（新增）

```json
{"prices": {"light": 1, "fine": 3},
 "maintenance": {"enabled": false, "message": ""},
 "styles": [{"id": "clear", "name": "冷白通透", "desc": "...", "tag": "热门"}]}
```

### `GET /api/templates`（新增）

模板商店数据源，小程序启动时拉取一次：

```json
{"groups": [{"id": "anime", "name": "动漫手办", "sort": 2, "enabled": true}],
 "items": [{"id": "t_ghibli", "group_id": "anime", "group_name": "动漫手办",
            "name": "吉卜力童话", "subtitle": "...", "cover": "https://...或/api/covers/...",
            "engine": "fine", "price": 0, "layout": "",
            "text_fields": [{"key": "title", "label": "主标题", "role": "title",
                             "default": "...", "max_len": 10}]}]}
```

只含启用的分组/模板；`prompt` 不下发。`cover` 已解析成可直接访问的 URL
（COS 签名直链 / `/api/covers/` 本地路径 / 外链）。

### `POST /api/rescue`（新增参数）

| 字段 | 类型 | 说明 |
|---|---|---|
| `template_id` | form | 可选；非空时引擎/提示词/输出尺寸/文字排版/价格全部以模板为准 |
| `text_fields` | form | 可选；JSON 对象字符串 `{"title":"..."}`，后端按模板字段规格截断并补默认值 |

`quality`/`style` 仍可用（不带 template_id 时走原有基础档位逻辑）。
响应里新增 `template_id` / `template_name` / `price`（本单实际单价，
小程序扣费以它为准）。`GET /api/jobs/{id}` 同样带这三个字段。

### `GET /api/covers/{filename}`（新增）

模板封面本地服务（配了 COS 后封面走 COS 签名直链，此接口仅开发期/降级用）。

### `GET /api/announcements`（新增）

```json
{"items": [{"id": "ab12cd34ef56", "title": "上新啦", "body": "...",
            "level": "info", "created_at": 1790522908.88}]}
```

只返回启用的公告，最新在前，最多 5 条。`level`: `info` / `warn` / `critical`。

### `GET /api/health`

```json
{"ok": true, "gateway": true, "fal": true, "baidu": false, "local": true,
 "configured": true, "chain": ["worldcodes", "fal", "baidu", "local"],
 "maintenance": false, "normalize_long_side": 1536,
 "max_upload_bytes": 26214400}
```

`configured=false` 表示所有 AI 后端都没配，全部走本地引擎。

### 用户身份与安全（上线必备）

- **登录**：小程序 `wx.login` → `POST /api/auth/login {code}` → code2session 换 openid →
  下发 hmac 签名 token（12h）。除健康检查/配置/公告/图片外，所有接口都要带
  `Authorization: Bearer <token>`；
- **内容审核**：上传原图与生成结果各过一道腾讯云内容安全机审（`cms/ImageModeration`，
  ≈ ¥0.0015/张），`Block/Review` 一律拦截并记审计；审核服务异常默认放行并大声记日志，
  后台「审核服务异常时也拦截」勾上即转为拦截（生产建议）；
- **频控/配额**：按 openid 每分钟 3 次、每日 20 次（后台可调；调试模式免每日上限）；
- **审计**：`backend/data/users.db`（SQLite）记录 login/submitted/completed/blocked/
  rate_limited 流水，合规要求留存 ≥6 个月；
- **COS 直传**：腾讯云 COS 三项配齐后自动启用 —— 小程序拿预签名 URL 直传原图
  （`PUT` 到 COS，不经服务器）、结果走签名直链下载，单请求公网流量从 ~1MB 降到 ~5KB；
- **用户数据**：`backend/data/users.db` 含审计与 openid，等同密钥待遇（已 gitignore）。

> 微信小程序后台需把后端域名与 COS 桶域名
> （`<bucket>.cos.<region>.myqcloud.com`）都加进 request/uploadFile/downloadFile 合法域名。
> mediaCheckAsync（微信官方免费机审）需要在公众平台配置消息推送回调才能收结果，
> 当前用腾讯云同步机审替代；要切换随时说。

### 管理后台 `/admin`

页面 `GET /admin`，API 前缀 `/admin/api`（cookie 鉴权 + `X-Admin-Request` 头）：

| 端点 | 说明 |
|---|---|
| `POST /admin/api/login` / `logout` | 密码登录，12h 会话；连续错 5 次锁 5 分钟 |
| `GET /admin/api/users` / `audit` | 用户列表与审计流水 |
| `GET /admin/api/overview` | 今日任务/成功率/成本、供应商状态、维护开关 |
| `GET` / `PUT /admin/api/settings` | 供应商配置、链路顺序、提示词、价格、维护（密钥打码返回） |
| `GET` / `POST` / `PUT` / `DELETE /admin/api/announcements[/{id}]` | 公告 CRUD |
| `GET` / `POST` / `PUT` / `DELETE /admin/api/template-groups[/{id}]` | 模板分组 CRUD（删除分组时组内有模板会拒绝） |
| `GET` / `POST` / `PUT` / `DELETE /admin/api/templates[/{id}]` | 模板 CRUD |
| `POST /admin/api/templates/{id}/cover` | 封面上传（multipart `file`，自动压到长边 720；COS 优先，本地降级） |
| `GET /admin/api/jobs?offset=&limit=` | 任务记录（provider、成本、错误、模板） |

> **安全提醒**：服务监听 `0.0.0.0` 时 `/admin` 会对公网暴露，
> 生产环境请在 nginx 层加 IP 白名单，或把 `HOST` 改成 `127.0.0.1` 走反代。
> `backend/data/settings.json` 内含密钥，已加入 `.gitignore`，等同 `.env` 待遇。

---

## 四、模板系统

管理后台 `/admin` →「模板」页，全部热调，**改完即生效，不用重启更不用编译**。
存储在 `backend/data/templates.json`（原子写、损坏自动回种子数据）。

### 数据模型

- **分组**：`id`（小写字母/数字/下划线）、名称、排序、开关；
- **模板**：名称、所属分组、副标题、**提示词**（≤4000 字）、引擎（light/fine →
  对应网关的 `model_light`/`model_fine`）、光子价格（0 = 按档位默认价）、
  输出长边（0 = 不处理；印刷级填 2048）、网关 `size` 参数（如 `2048x2048`，
  网关不支持会自动忽略并降级）、**模型覆盖**（填供应商的原生 2K/4K 模型名即可
  单模板换模型）、文字排版预设 + 字段（≤6 个）、排序、开关、使用次数统计。

种子数据已含 4 组 9 个模板：修复增强（原三档风格迁移 + 深度超分）、
动漫手办（吉卜力/粘土/3D 手办盲盒）、海报日签（复古电影海报 + 中文标题排版）、
明信片贺卡（旅行明信片，2K 印刷级 + 落款排版）。

### 任务管线里的模板

提交时带 `template_id` 的任务：模板提示词替代档位提示词 → 按降级链执行
（网关带模板的模型覆盖与 size 参数）→ 本地兜底 → 按模板 `output_size`
统一输出长边（Lanczos）→ 按模板排版预设叠加用户文字。模板参数用
**提交那一刻的快照**，后台随后改动不影响在途任务。

### 文字排版（为什么是代码叠字，不是模型画字）

生成模型至今排不稳长段中文。海报/明信片类模板的提示词都写明
"画面本身不要包含任何文字"，用户文字由 `text_overlay.py` 用 Pillow 叠加：
字体自适应缩放、超宽折行、三种预设（明信片底部卡带 / 海报下三分之一居中 /
邮票右上角标）。字段默认值支持 `{today}`（自动换成当天日期）。

### 示例图（封面）与带宽

封面上传后自动压成长边 720 的 JPEG（单张 60~150KB），存储策略：

1. **配了 COS（推荐）**：传到 `covers/{id}_v{n}.jpg`，`/api/templates`
   返回时**现场预签名**（纯 HMAC 计算，零网络请求），图片流量全走 COS/CDN，
   后端只出几 KB 的 JSON。版本化对象键天然刷缓存；
2. **未配 COS**：落盘 `data/covers/`，走后端 `/api/covers/`（带长缓存头），
   开发期够用，生产建议配 COS；
3. 也可在后台直接粘贴外链/CDN 地址。

小程序端 `image` 组件 `lazy-load`，按分组懒加载。

### 占位封面

`backend/tools/gen_covers.py` 给没有封面的模板生成分组主题色占位图
（带 PLACEHOLDER 水印，提醒用真实管线出图替换）：

```bat
cd backend && .venv\Scripts\python tools\gen_covers.py
```

**正式上线的封面应该用对应模板真实跑几张图挑最好的一张**，在后台上传。

---

## 五、环境变量

全部有默认值，可以整段不写。见 `backend/.env.example`。

| 变量 | 默认 | 说明 |
|---|---|---|
| `ADMIN_PASSWORD` | 空 | 管理后台密码；留空则首次启动自动生成并写回 `.env`。改后需重启 |
| `WX_APPID` / `WX_APP_SECRET` | 空 | 仅作首次迁移，之后后台管理 |
| `TENCENT_SECRET_ID` / `TENCENT_SECRET_KEY` | 空 | 内容审核 + COS 共用，仅作首次迁移 |
| `FAL_KEY` | 空 | 仅作首次初始化迁移用，之后密钥在后台管理 |
| `BAIDU_API_KEY` / `BAIDU_SECRET_KEY` | 空 | 同上 |
| `HOST` / `PORT` | `0.0.0.0` / `8000` | 监听地址 |
| `WORKERS` | `4` | 线程池大小 |
| `MAX_UPLOAD_BYTES` | 26214400 | 上传体积上限（25 MB） |
| `MAX_PIXELS` | 60000000 | 像素上限 |
| `NORMALIZE_LONG_SIDE` | 1536 | 仅作首次默认值，之后在后台热调 |
| `JOB_TTL_SECONDS` | 2592000 | 任务保留（30 天） |
| `JOB_MAX_ENTRIES` | 5000 | 进程内最大任务数 |
| `ALLOWED_ORIGINS` | `*` | CORS 白名单，逗号分隔 |
| `LOG_LEVEL` | `INFO` | 日志级别 |

---

## 六、本轮完成的优化

### 安全

1. **移除硬编码密钥。** 原先 `main.py` 第 28–31 行把百度 AK/SK 明文写死在代码里，现已全部改走环境变量。**那两个密钥曾经公开暴露过，务必去百度控制台轮换。**
2. **恢复 TLS 证书校验。** `baidu_ai.py` 原本用 `ssl.CERT_NONE` + `check_hostname=False`，等于把 `access_token` 明文暴露给任何能劫持链路的人。
3. **路径穿越防护。** `/api/images/{filename}` 现在做 basename + 白名单正则 + `realpath` 双重校验；`job_id` 只接受十六进制。
4. **上传体积边读边校验**，不再把超大文件整个吃进内存。
5. **CORS 修正。** `allow_origins=["*"]` 与 `allow_credentials=True` 是非法组合，浏览器会直接拒绝 —— 现在二者互斥。

### 正确性

6. **修掉降级链的崩溃。** 原 `main.py` 调用 `engine.process(..., style=style)`，但 `engine.process` 的签名是 `quality`，本地兜底一跑就 `TypeError`。
7. **fal.ai 参数对齐官方 schema。** 逐个字段核对过 OpenAPI：
   - `clarity-upscaler` 的字段名是 `upscale_factor`，不是 `scale_factor`
   - 该模型**没有** `downscaling` / `output_format` 参数
   - 补上 `guidance_scale`、`enable_safety_checker`
   - `esrgan` 补上 `model` 权重选择和 `output_format`
8. **fal 队列状态枚举修正。** 实际是 `IN_QUEUE` / `IN_PROGRESS` / `COMPLETED`，原实现只认 `COMPLETED`，遇到失败态会死等到超时。
9. **异步化。** 提交立刻返回 `job_id`，处理在线程池跑；`JobStore` 带锁 + TTL 清理。
10. **双重压缩修复。** `_finalize` 检测到上游已是 JPEG 就直接搬，不重编码。
11. **归一化输出改 JPEG。** 原先存 PNG，1536 长边动辄 3~5 MB，转 data URI 还要再涨三分之一；q95 JPEG 只有几百 KB。
12. **统一尺寸后关掉 2K 上采样。** 归一化已经把长边定到 1536，本地引擎再插值回 2000 纯属放大噪声。
13. **日志替代 print。** `engine.py` 里 5 处 `print` 改 `log.debug`，可用 `LOG_LEVEL` 控制。
14. **`lifespan` 替代废弃的 `on_event`。**

### 健壮性

15. **fal 客户端**：指数退避重试（只对 5xx / 429 / 网络错误）、`last_notice` 记录降级原因、超限自动压 JPEG、缺文件提前报 `NO_INPUT`。
16. **百度客户端**：`access_token` 刷新加锁（原先多线程会互相覆盖）、401 自动刷新重试、Session 复用连接、4 MB 上限自动压缩、错误码分类。
17. **`fal_ai.py` 不硬依赖 OpenCV**，`cv2` 延迟导入。

### 小程序

18. **修掉最大的一致性 bug。** 后端已改成异步，小程序却还在拿 `data.result_url` 直接跳转 —— 那时结果图根本还没生成，必然 404。新增 `utils/api.js`，提交后轮询到 `succeeded` 再跳。
19. **`token` 持久化。** 原先 `onLaunch` 无条件把鱼干重置成 9999，消耗逻辑是空实现。现在是「首次赠送 5 条 + `wx.setStorageSync` 持久化 + 真实扣减」。
20. **拖拽滑块性能。** 每次 `touchmove` 都 `setData`，且容器尺寸只在 `onReady` 量一次（旋转/分屏后失准）。改为触摸前重新测量 + 百分比未变就不 `setData`。
21. **后端健康检查**，首页状态栏如实显示「后端就绪 · fal.ai」或「未连接」。
22. **busy 态防重复提交**，档位卡与上传按钮在请求期间置灰。
23. **结果图加时间戳**，避开小程序图片缓存拿到旧图。
24. **保存相册**加 `saving` 互斥、处理用户取消、演示模式明确提示。

---

## 七、已知限制

- **任务落盘 SQLite（jobs.db）**，成功记录可跨重启恢复，在途任务按实际扣款流水幂等退款。调度缓存仍要求单进程；横向扩容需共享队列与账本。
- **扣费在后端 SQLite 账本**（`users.balance`）：提交时预扣、失败自动退款，签到/看视频/邀请奖励全部服务端记账，客户端本地数据改了没用。
- **微信小程序 `wx.request` 域名必须 ICP 备案**，所以小程序不能直连 fal.ai，生产环境需要一台备案服务器做代理，或走微信云开发/云托管（云函数可调任意域名）。
- **`backend/tools/realesrgan.zip` 是 0 字节**，自建超分那条退路没下载成功。现在用不上（走 API），要保留的话需重新下载。
- **`requirements.txt` 里的 `opencv-python` 无版本上限**，当前环境装的是 5.0.0，已验证可用。

---

## 八、验证记录

| 项目 | 结果 |
|---|---|
| Python 编译（4 个模块） | 通过 |
| 小程序 JS 语法（4 个文件） | 通过 |
| 端到端冒烟（light + fine 两档） | 通过，输出 1152×1536 |
| fal 参数 vs 官方 schema | 3 个用例全部零未知键、零缺必填 |
| 路径穿越 / 非法格式 / 非法档位 | 全部正确拒绝（404 / 400） |
| 硬编码密钥残留扫描 | 干净 |

密钥未配置时，两档都能跑通降级链并产出结果 —— 这是最重要的保证：**外部服务挂掉不会让功能不可用。**

### 模板系统轮

| 项目 | 结果 |
|---|---|
| Python 编译（templates_store / text_overlay / main / admin_api / gateway_ai / gen_covers） | 通过 |
| 小程序 JS（index.js / api.js）+ admin.html 内嵌 JS | 通过 |
| `GET /api/templates` | 4 组 9 模板，封面 URL 已解析，prompt 不下发 |
| 分组/模板 CRUD + 校验（非法名、组内有模板拒删） | 全部按预期拒绝/通过 |
| 封面上传 → `cover_url` 解析 → `/api/covers/` 取图 | 通过 |
| 明信片模板管线：本地引擎 + 2K 输出（2048×1536）+ 底部卡带排版 + `{today}` | 通过 |
| 海报模板管线：下三分之一居中排版 / 邮票角标排版 | 通过 |
| 未登录 `POST /api/rescue` | 被登录门槛拦截 |


## 八、2026-09 修复后的运行约定

- `users.db/job_charges` 保存实际扣款、配额预留和退款状态。免费任务记录扣款0；退款与邀请码绑定均按事务和唯一标识去重。`jobs.db` 自动追加字段，不重建或清空用户数据。
- 旧版本在途任务没有实际扣款流水时，标价不能证明是否扣费；新版本不凭标价增发退款。历史任务余额应结合原账务资料核对。新任务及新版本进程中断由账本自动恢复。
- `/api/images` 使用10分钟文件签名或所属用户会话；状态查询每次返回新地址。原图、对比图与结果从完成起统一保留30天；用户主动删除时立即删除，远端失败保留重试队列。
- 小程序对比页进入/回到前台时刷新任务信息，图片从 COS 下载为页面内临时文件，避免签名过期导致半屏空白。403 只换新签名重试一次；不使用业务服务器图片代理。404 或历史本地存储地址会请求补存接口，它只返回 JSON：检查原对象，若缺失但本地成品仍在，仅通过 COS 内网补存到新的对象键（避开旧删除队列），然后小程序直连 COS 下载。没有本地副本或内网不可达时明确报错，不改走服务器图片代理/公网补存。
- COS 配置启用时，结果上传并确认对象存在后才标记成功；失败会重试一次，仍失败则任务失败并退还实际扣款。原图上传失败不再连带跳过结果上传。后端独立内网域名已纠正为 `<bucket>.cos-internal.<region>.tencentcos.cn`；`.myqcloud.com` 不支持这种独立内网域名形式。参见[腾讯云域名说明](https://cloud.tencent.com/document/product/436/67768)。
- 保存优先复用已下载的成品，不等待原图、不重复下载；本地临时文件被微信清理时重新从 COS 下载一次。下载域名、COS HTTP 错误、相册权限、相册保存错误分别提示，不再统一显示“网络超时”。`photo.myil.top` 必须同时配置为小程序的 request 与 downloadFile 合法域名，且 HTTPS 证书有效。
- 新照片的纯修复默认轻量档；模板按自身引擎档位计费。模板模式不提供画幅选择，上传素材和模型生成成品都不按输入比例强制裁切；非模板修复仍支持用户选择画幅。
- 后台用户“删除”只清理管理列表及其人数统计。账户、余额、邀请码、作品和封禁状态保留；同一用户再次登录或持有效会话使用接口时恢复显示，不重复领取新用户赠送。
- `DELETE /api/my/jobs/{job_id}` 删除本人作品；`DELETE /api/my/jobs` 清空本人作品。删除状态持久化，排队任务按实际扣款退款，工作线程不会重新交付已删除作品。
- `cleanup.db` 持久化本地/COS删除与到期任务，后台每30秒检查；失败指数退避重试。COS配置需具有删除对象权限。旧数据库可定位的对象会补入队列；桶内旧版本未记录的上传/审核临时对象建议设置独立生命周期规则。
- 两种Docker构建均只复制代码和静态页面/logo，排除`.env`、数据库、上传、日志和工具。运行时挂载`/app/data`与`/app/uploads`，用环境变量注入`ADMIN_PASSWORD`等配置；不要把本机运行数据放入镜像。
- Docker与Linux部署安装`fonts-noto-cjk`，也可用`TEXT_FONT_PATH`指定中文字体。未找到中文字体会明确报错，避免返回缺字海报。

### 激励视频凭据验证

仅客户端`onClose/isEnded`不能作为发奖证明。服务端完成事件和签名应由广告平台/可信桥接服务独立核验（参见[平台回调流程](https://uniapp.dcloud.net.cn/uni-ad/ad-rewarded-video.html#callbackweixin)）。本项目不把客户端布尔值变成“已验证”事件。

后台“通用设置”需同时设置广告位、HTTPS验证服务地址、验证服务密钥后才返回`rewarded_video_ready=true`。客户端广告桥接SDK在关闭回调中提供`receipt`或`transactionId`，发送至`POST /api/me/earn`的`receipt`字段。没有凭据时保持未到账状态；原生SDK仅提供`isEnded`时，应先接入平台认可的服务端验证桥接。

项目后端向配置的可信验证服务发送：

```json
{"openid":"用户的服务端身份","ad_unit_id":"广告位ID","receipt":"平台观看凭据"}
```

请求带服务端`Authorization: Bearer <verifier_key>`，密钥不会返回小程序。验证服务必须先独立核对平台签名、事件完成状态和归属，再返回：

```json
{"verified":true,"openid":"同一用户身份","ad_unit_id":"同一广告位ID","event_id":"平台唯一完成事件ID"}
```

未验证、身份不符、服务故障或跨用户重放都拒绝发奖；同一用户重试相同已验证事件不会重复到账。每日3次上限仍然适用。代码支持此验证契约，真实广告桥接、COS和AI供应商需用各自配置作集成验收。

### 回归测试

测试复制代码到独立临时目录，只使用合成图片和临时SQLite；外部请求被禁止或使用受控模拟。

```powershell
$env:PYTHONIOENCODING='utf-8'
python F:/Project/image/backend/tests/run_review.py
```

输出和逐项结果默认保存在`audit/regression/`。完整修复记录及代码回退副本在`audit/2026-09-29/fixes/`。回退只恢复代码，不回写用户数据库或图片。
