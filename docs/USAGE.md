# CourseDeck 使用与开发

返回 [README](../README.md)。Inbox 与自定义待办：[连接方式、规则和当前范围](mail.md)。

本地优先、单用户、只读的大学作业聚合器。浏览器访问 localhost；一个 Python 进程提供
API 和已构建的 React UI。数据、笔记和浏览器 profile 留在本机，不需要账号系统、远程后端或常驻服务。

**当前状态：早期开发版。已使用个人 NYU 账号验证主要来源；具体覆盖范围见下文。**

## 启动

完成首次安装后，Windows 日常启动：

```powershell
.\start.cmd
```

然后打开 **http://127.0.0.1:48321**。关闭运行窗口或按 Ctrl+C 即停止；不安装 Windows Service。
已保存数据会立即出现，随后后台同步已连接来源。

在新的 checkout 首次安装（需要 Python 3.12+、uv、Node.js 20.19+ 或 22.12+）：

```powershell
.\setup.cmd
.\start.cmd
```

跨平台手动执行：

```sh
uv sync --locked
cd frontend
npm ci
npm run build
cd ..
uv run playwright install chromium
uv run coursedeck
```

日常运行不需要 Node.js 服务。可用 `uv run coursedeck --port 48322` 更换端口。
Linux 可能还需要 `uv run playwright install-deps chromium` 安装浏览器系统库。

第一次打开，使用 **Sources** 连接平台。应用仅显示真实来源；旧版演示数据会在升级时移除。

## 功能与验证边界

| 能力 | 当前实现 | 验证情况 |
| --- | --- | --- |
| Core | SQLite、upsert、缓存优先、并行同步、历史、备份 | 独立测试数据 + 实际 Chromium |
| Todo / Courses / Sources / Settings | 搜索、来源筛选、截止时间分组、详情、来源链接 | 桌面和手机浏览器实测 |
| Google Classroom | 默认专用浏览器登录；官方只读 API 可选 | 真实账号：1 门课程、1 份作业、精确截止时间及 Assigned 状态；API 仍为模拟测试 |
| Gradescope | 独立 profile、SSO/2FA、会话 HTTP、学生课程/作业 HTML parser | 真实账号验证：2 门课程、4 项作业；另有合成 fixture 与 profile 复用测试 |
| WebAssign | 独立 Chrome 登录、OS vault 恢复会话、全部作业列表 | 真实账号验证：1 门课、5 项作业及截止时间，关闭窗口和重启服务后同步成功 |
| Brightspace | 浏览器恢复学校 SSO、作业提交区、带截止时间的课程内容及分页 | NYU 实测：7 门课程、33 项任务；与 Work To Do 页面核对 |

WebAssign 读取课程首页进入 My Assignments → All Assignments，支持目前实测的英文日期格式。
会发现页面中的课程链接，也支持配置多个课程 URL；多课程选择器和其他语言尚待真实账号验证。
提交状态无法从列表可靠确定，保持 `unknown`，因此同步报告 `partial`，不参与自动归档。

**Brightspace 当前覆盖 assignment dropboxes、可读取的本人提交/成绩及带截止时间的课程内容。**
同步前先打开主页，等待学校 SSO 自动恢复后再请求数据。内容遍历包含所有分页，读取内容完成
不等于作业提交；未知状态会保留。独立测验、讨论、公告和正文内文字截止时间尚未覆盖，
因此明确报告 `partial`，也不触发归档。单个接口的 403 会标明对应作业或课程，不一律视作账号掉线。
浏览器 API 是否允许直接使用会话 cookie 由学校实例决定；被禁止时尝试保守的 DOM 回退。
API token 的首次签发由学校授权的 OAuth app 完成，目前没有内置 Brightspace 授权注册向导。

Gradescope 只读学生课程页面；没有采用密码 API 登录、教师写操作或日常 Chrome Cookie 数据库。
对旧课程不可见的账号页不会宣称是完整快照。未知页面结构、登录过期和零条数据有不同处理。

## 连接平台

### Google Classroom

默认不需要 Google Cloud 项目：

1. Sources → Google Classroom → **Connect**。
2. 在专用浏览器中登录学校账号，完成 SSO / 2FA，进入 Classroom 课程页。
3. 回到 CourseDeck 点击 **Finish login**，验证成功后保存会话并同步。

浏览器读取目前为实验性：读取可见课程、Classwork 中的 Assignment 及详情，监听页面自己发出的响应
提取与作业 ID、课程 ID、标题一致的精确 UTC 截止时间，不单独重放内部接口。无法确认的字段保留旧值，
没有旧值时保持未知。归档课程、Question、成绩和其他语言的页面尚未覆盖，
不会把未出现的作业自动归档。Google 或学校可能拒绝此浏览器登录；此时不视为连接成功。
不会读取日常浏览器的 cookie 或绕过登录限制。

可选官方 API：配置中选择 `official_api` 并保存，再次打开配置填写客户端信息。
已有 API 配置的安装保留原连接方式。

1. 在自己的 Google Cloud 项目启用 **Google Classroom API**。
2. 配置 OAuth consent screen；开发测试状态下把自己的 Google 账号加入 test users。
3. 创建 **Desktop app** 类型的 OAuth client，取得 client ID / client secret。
4. Sources → Google Classroom → 配置，填写这两项。它们进入 OS credential store。
5. 点击 Connect，在系统浏览器中授权；回调后返回 CourseDeck。此来源不需要 Finish login 按钮。

仅申请：

```text
https://www.googleapis.com/auth/classroom.courses.readonly
https://www.googleapis.com/auth/classroom.coursework.me.readonly
```

读取自己的课程、作业和提交/成绩，不申请名单、邮箱或写权限。支持分页与 refresh token。
Google `dueDate` / `dueTime` 按官方 UTC 语义合并，不按本地午夜猜测。
学校可能限制第三方授权；OAuth consent screen 为 Testing 时，Google 也可能限制 refresh token
有效期。授权过期后在 Sources 重新连接。

Desktop client 使用启动端口上的 loopback callback，例如
`http://127.0.0.1:48321/api/oauth/google_classroom/callback`。不要用 Web application 类型代替。
回调验证随机 state、十分钟有效期与 PKCE。不会在日志中记录 code、access token 或 refresh token。
Disconnect 删除本地 token 和专用浏览器会话；保留客户端配置以便重新连接。若要在 Google 侧撤销应用授权，
使用 Google 账号的第三方连接管理页面。

### Gradescope

1. Sources → Gradescope → Connect。
2. 在打开的专用 Chromium 中手动登录，包括学校 SSO/2FA。
3. 回到 CourseDeck 点击 **Finish login**，程序验证会话，关闭登录窗口后开始后台同步。
4. 会话过期时使用 **Reconnect**。在登录窗口内完成操作期间不会同时占用该 profile 同步。

可在设置里指定 Gradescope 实例 URL，以及该站显示的 IANA 时区。已有 offset 的时间直接转 UTC；
没有 offset 又缺少源时区、或遇到歧义 DST 的时间会报 warning，不猜测。
**Disconnect 会重置该平台的专用 profile，保留已保存的课程、作业、笔记。**

### WebAssign

1. Sources → WebAssign → Connect，完成 Cengage 登录或学校跳转。
2. 点击 **Open WebAssign**，进入含 My Assignments 区块的课程首页，再点击 Finish login。
3. 如有课程未被发现，可在配置的 **Assignment list URLs** 中填写课程首页 URL 的 JSON 数组；
   不要复制 UserPass 等认证参数。连接器将认证参数和会话 Cookie 保存在 OS vault。
4. 已支持实测的英文日期及 EDT/EST 等明确时区；其他格式可配置源时区和日期格式。

WebAssign 存在逐题提交，当前不会把部分分数推断为整份作业完成，提交状态保持 `unknown`。
解析器不读取题目答案、不点击提交。自动归档关闭。

### Brightspace

在 Sources 的配置中填写学校实例的 HTTPS 根地址，例如 `https://school.brightspace.com`。
不要填写含会话 token 的登录跳转 URL。

- **Browser（默认）**：Connect → 手动 SSO → Finish login。无需注册 OAuth app。
- **API**：使用学校授权得到的只读 access token。可附 refresh token、client ID / secret
  以支持刷新。全部敏感值进入 OS vault；默认 token 剩余有效期为 3600 秒，应按实际填写。

API 最小 scope 包括 `enrollment:own_enrollment:read` 与 `dropbox:folders:read`；
refresh 还需要学校 OAuth 配置支持。版本默认 LP `1.49`、LE `1.82`，学校环境不同可调整。
切换实例或 transport 时清除已有 API 授权，防止旧 token 被发给另一个实例。

## 数据与恢复

默认相对于项目目录存储：

```text
data/
  coursedeck.sqlite3          # courses、tasks、local states、connector states、sync history
  browser-profiles/
    google_classroom/
    gradescope/
    webassign/
    brightspace/
  backups/                   # Settings → Create backup
  debug/                     # 仅手动 smoke 测试截图；应用不默认保存真实页面
```

可通过环境变量 `COURSEDECK_DATA_DIR` 指定绝对路径。OAuth 凭据在 OS vault 中，不在 SQLite
或普通 JSON 文件里。Windows 长 token 分段保存，全部片段就绪后才发布新版本，失败保留旧版本。
不支持明文 keyring fallback。WebAssign 浏览器模式也需要 OS vault 保存恢复会话所需的凭据。

恢复步骤：

1. 完全停止 CourseDeck。
2. 将当前整个 data 目录重命名为一个保留目录。
3. 创建新的 data 目录，把所需 backup 复制为 `data/coursedeck.sqlite3`。
4. 启动。课程、任务、本地笔记和偏好恢复；如果没有恢复浏览器 profiles，重新连接浏览器来源。

不要把旧数据库的 `-wal` / `-shm` 文件与备份文件混合。数据库备份使用 SQLite online backup
API，运行中可生成一致快照。备份不包含 browser profiles 或系统凭据库。
更换 data 绝对路径时，OS vault 命名空间也会变化，需要重新授权。

数据目录、profile、调试产物和常见凭据文件均已 gitignore。数据库和备份含个人学习内容，
请按私人文件管理；应用不会上传它们。

## 同步语义

- 每个平台有独立锁，平台间并行；超时/异常隔离。启动和手动同步受同一把锁控制。
- 以转义后的 `provider:course:assignment` 身份 upsert。`first_seen_at` 保留，`last_seen_at` 更新。
- 本地隐藏、dismiss、pin、note、priority 存于独立表；绝不写回 LMS。
- `due_at` 与 `closes_at` 始终分开。无法解析新的时间时，保留此前已知时间并记录 warning。
- 只有 `success + complete` 才累计 missing；连续至少三次完整同步缺席并且至少七天未见，
  才自动 archive。任何失败/partial 都不会推进 missing。重新出现自动恢复，笔记不变。
- 不物理删除任务。Archive 和 Hidden 都有独立查看入口。
- UI 每三秒向本地后端读取快照，不意味着每三秒访问 LMS；V1 只在启动/手动操作时同步 LMS。
- 右上角独立检测本地服务心跳，超时显示 Disconnected，恢复后自动回到 Connected。
- 界面固定深色。侧栏 COURSES 的 + 可新建课程；Courses → Edit course 可添加多个来源课程。
- 一个来源可包含多门课程，同一本地课程可关联多个来源；每个来源课程只归属一个本地课程。
- 本地课程名称独立保存，后续同步不会覆盖；绑定后的任务仍沿用原身份和本地笔记。
- Courses 中可以禁用或删除课程。禁用会从侧栏和待办隐藏；删除的课程在 Show deleted 中恢复。
  两者均保留缓存、笔记、邮件和来源关联，后台同步不会撤销本地状态。
- Edit course 的 Alias 可设置别名；课程卡右上角的 `*` 表示别名已启用。Clear alias 后保存，
  恢复主来源的当前课程名；尚未关联来源的课程恢复创建时的名称。
- Sync history 保留最近 1000 次；界面展示最近记录。Debug 信息不包含任务正文、笔记、token 或网页内容。

## 开发与测试

```sh
uv run pytest
uv run ruff check
uv run ruff format --check
cd frontend
npm run typecheck
npm test
npm run format:check
npm run build
cd ..
```

前端开发：先 `uv run coursedeck`，另一个终端 `cd frontend && npm run dev`。
Vite 仅监听 loopback；开发代理默认后端 48321。生产环境由后端提供静态资源，不需要 CORS。

真实浏览器 smoke（前端需先构建；UI 测试自行启动临时数据目录和独立端口）：

```sh
uv run python scripts/smoke_ui.py
uv run python scripts/smoke_profiles.py
uv run python scripts/smoke_vault.py
```

`smoke_ui.py` 只操作合成的临时数据，验证课程绑定、笔记、心跳拒绝/超时/恢复、深色桌面与手机布局；
截图保存在 data/debug，不会修改当前工作区的真实数据库。
`smoke_profiles.py` 在临时目录验证独立 profile 的 cookie 跨启动持久化及清理。
`smoke_vault.py` 写入后删除随机命名的合成 OS vault 凭据，不读取学校凭据。
`tests/fixtures` 全部是合成样本，不是从个人学校账号抓取的网页。

## 扩展 connector

实现 `Connector` 生命周期与 `SyncResult`，在 `connectors/registry.py` 注册。把网络 transport
和纯 parser/mapper 分开；统一引擎不理解 cookie、OAuth、HTML 或平台 API。
连接配置字段由 connector 声明，UI 只渲染通用表单。
新增 parser 必须有 fixture；未验证全量覆盖的同步必须保持 `complete=False`。

进一步说明见 [architecture](ARCHITECTURE.md)、[connector research](CONNECTORS.md)、
[verification record](VERIFICATION.md) 和 [third-party notices](../THIRD_PARTY_NOTICES.md)。
