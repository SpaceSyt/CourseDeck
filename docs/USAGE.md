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
| Core | 两个本地 SQLite 数据库、upsert、缓存优先、串行来源同步、有限重试、变化日志、校验备份与恢复 | 独立测试数据 + 实际 Chromium |
| Chat / Todo / Changes / Courses / Sources / Settings | 资料检索、任务提问、来源变化、证据关联、规则预览和课程管理 | 桌面和手机浏览器实测；Chat 模型调用使用模拟接口验证 |
| Google Classroom | 默认专用浏览器登录；官方只读 API 可选 | 真实账号：1 门课程、2 份作业，已核对已提交、无截止日期及精确截止时间；API 仍为模拟测试 |
| Gradescope | 独立 profile、SSO/2FA、学生课程/作业 DOM | 真实账号验证：2 门课程、4 项作业；另有合成 fixture 与 profile 复用测试 |
| WebAssign | 独立 Chrome 登录、OS vault 恢复会话、全部作业列表 | 真实账号验证：1 门课、6 项作业及截止时间；来源会话过期后仍可能需要重新登录 |
| Brightspace | 浏览器恢复学校 SSO、作业、明确截止的课程内容、测验和讨论；独立资料采集读取 TOC、正文/PDF 和 News | NYU 实测任务与学生状态；本轮资料读取范围与剩余缺口见验证文档 |

WebAssign 读取课程首页进入 My Assignments → All Assignments，支持目前实测的英文日期格式。
会发现页面中的课程链接，也支持配置多个课程 URL；多课程选择器和其他语言尚待真实账号验证。
仅有分数不能证明整份作业完成。普通、无访问限制的 Homework 可只读核对所有小题的提交次数；
只有覆盖全部小题且每个小题均有提交记录时才识别为 submitted，当前撤回或重新开放标记优先。
受限或可能计时的作业不自动进入；列表没有状态、详情无法完整核实时保持 `unknown`。

**Brightspace 任务同步覆盖作业提交区、有明确结构化截止的课程内容、独立测验及明确截止的讨论主题。**
同步前先打开主页，等待学校 SSO 自动恢复后再请求数据。内容遍历包含所有分页和完成项。
有明确任务或结构化截止的内容继续显示在 Todo；普通讲义与公告进入独立资料库，不从正文日期猜测新任务。
打开 LTI、讲义或作业链接不等于提交；任务只有可核实且适用于该项目的来源状态才能进入 Done。
测验包括历史/已完成项；尝试接口无权限时读取本人 Quiz Summary 的 Completed 次数。
受限作业可使用同一课程的学生列表状态补读；同名歧义、页面身份不符或状态冲突不会强行匹配。
资料采集遍历课程 TOC，并读取可访问正文、同课程静态 PDF/HTML/纯文本文件及 News 公告；读取文件不点击课程完成按钮。
PDF 使用本地文本提取，不自动解释图表或把正文日期改写成截止时间。未开放、权限受限、扫描件或不支持的
文件仍显示读取异常，保留已知截止时间和缓存正文；详情中的空白不再暗示原始内容为空。
讨论使用 LE 1.90 的明确 DueDate，关闭时间不冒充截止时间。公告和正文中的文字日期不会自动生成或修改任务。
读取权限、预算、分页或字段缺口会报告 `partial`，也不触发归档。单个接口的 403 会标明对应作业或课程，不一律视作账号掉线。
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
没有旧值时保持未知。归档课程扫描已加入，目前仅空归档页通过真实账号验证。
已完成卡片仍会读取；Turned in late 识别为已提交。页面记录明确无截止日期时清除旧截止时间，
单纯读不到日期仍保留缓存。
读取作业正文与明确可见分数。独立资料采集补充 Classwork 材料、Stream 公告，以及可访问 Google Docs 的正文；
Question、其他附件、折叠/历史内容和其他语言尚未完整验证，附件内日期不会写回任务。
读取预算、未展开内容或访问失败保留警告和旧缓存，不会把未出现的作业自动归档。
Google 或学校可能拒绝此浏览器登录；此时不视为连接成功。
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

WebAssign 存在逐题提交。普通作业会核对完整的小题提交记录，不从部分分数推断完成；
受限作业、页面结构不明或提交记录缺失时保持 `unknown`。解析器不提取题目答案、不点击提交，自动归档关闭。

### Brightspace

在 Sources 的配置中填写学校实例的 HTTPS 根地址，例如 `https://school.brightspace.com`。
不要填写含会话 token 的登录跳转 URL。

- **Browser（默认）**：Connect → 手动 SSO → Finish login。无需注册 OAuth app。
- **API**：使用学校授权得到的只读 access token。可附 refresh token、client ID / secret
  以支持刷新。全部敏感值进入 OS vault；默认 token 剩余有效期为 3600 秒，应按实际填写。

API 最小 scope 包括 `enrollment:own_enrollment:read` 与 `dropbox:folders:read`；
refresh 还需要学校 OAuth 配置支持。版本默认 LP `1.49`、LE `1.82`，学校环境不同可调整。
切换实例或 transport 时清除已有 API 授权，防止旧 token 被发给另一个实例。

## 变化与任务关联

**Changes** 显示新增任务、截止提前/延后、标题/要求变化、重新开放、字段不可读和来源缺失/恢复，
保留前后值、观察时间与来源链接。可按课程、来源、类型、未读或 Important 筛选；
**Mark shown read** 只标记当前页，单条也可恢复未读。Todo 会提示尚未读的重要变化。

首次升级把已有缓存作为基线，不把旧任务全部报成新增；之后新发现的任务与实际变化才入日志。
重复相同快照不会重复生成同一变化，读取失败也不会被当成截止或正文已改变。
来源从已确认完成重新变为开放会记录 **Reopened**；标记变化已读不等于完成任务，
也不会清除用户自己的本地勾选、隐藏或笔记。

任务详情的 **Related items** 可打开相关任务、邮件或资料。明确的任务级来源 ID/链接可以直接建立关联；
同课作业编号等较弱或有歧义的证据只显示 **Suggested · Needs confirmation**，需 Confirm 或 Dismiss。
**Link item** 可人工添加，**Unlink** 解除关联，**Show dismissed links and history** 查看并恢复已排除的链接。
Evidence 展示匹配依据。关联不是合并：各来源的截止、提交状态和本地笔记独立，一处完成不会传播到其他来源。

## 邮件规则预览

在 Inbox 菜单打开 **Mail rules**，或从邮件详情的 **Create rule** 以该邮件为起点填写规则。
新增、编辑、启停、删除及恢复默认规则会先展示预览：匹配邮件、前后分类、新增忽略、恢复、优先级变化与冲突。
点击 **Apply changes** 才保存；取消预览不改规则。预览只基于当前邮件缓存，正文不完整时会提示可能漏匹配，
不能把预览计数当作完整邮箱的影响范围。手工课程选择和手工恢复等本地决定保留。

规则可勾选 **Mark as priority**；默认关键词包括 action required、survey、please reply、response required
以及明确延期/截止变更用语。优先标记不会自行创建任务或修改截止。Inbox 默认仍按最新时间排列，
可在菜单选择 **Priority only** 或 **Priority first**；后者在优先组内保留时间顺序。

## Chat 与资料库

侧栏 **Chat** 位于 Todo 上方。同页的 **Materials** 不需要模型配置：选择课程或 All courses，
搜索本地资料，查看命中片段、展开全文并打开来源。除课程外，可按来源、资料类型、正文读取时间
（最近 7 天、更早或未知）及完整性筛选。搜索支持中英文关键词和引号短语，列表给出相关片段。
读取时间依据正文实际取得时间，失败检查不会把旧正文变成新资料；Complete 只表示该条提取状态，
不是整个课程已同步完整。列表条数只代表当前索引，读取预算、缺失正文和来源覆盖警告需要一起查看。

Google Classroom 与 Brightspace 完成课程同步后，会在同一来源锁内自动收集支持范围内的资料。
采集存在条数、正文读取和时间预算；命中预算不等于课程已读完。失败时保留旧正文及其原读取时间，
同时记录本次异常。扫描 PDF、图片、受限附件和未适配内容不会被静默视为空白。
资料与作业是不同记录：公告和讲义不会仅因包含日期而变成待办，也不按标题相似度合并任务。

要使用模型对话：

1. Settings → **Chat API** 填写 OpenAI-compatible **Base URL**（通常包含 `/v1`）和 **Model**。
2. 如接口需要认证，在 **API key** 填写密钥并保存；本地无认证接口可不填。
3. 返回 Chat 选择课程，或在任务详情点击 **Ask about task**。后者展示任务上下文和 Related materials，
   区分已有链接与关键词匹配；仅打开页面不会发送模型请求。**Find materials** 可在提问中请求补取相关资料。
4. 点击回答中的编号查看本地依据，通过旁边的来源链接核对原文。历史会保留引用和读取异常。

API key 只写入系统凭据库；加载设置不会返回密钥。已有密钥时留空表示保留，**Remove saved key** 才显式删除；
更换接口地址会清除旧密钥，避免发给另一个服务。系统凭据库不可用会显示异常，不会伪装成未配置。
**Built-in prompt** 可展开查看，当前不提供编辑入口。

只有用户发送 Chat 消息时才调用模型，课程/邮件同步和本地资料检索完全不依赖 AI。
本轮对话及相关课程证据会发送到配置的接口；使用远程接口时，这些内容会离开本机。
对话工具可查找作业、按需读取关联链接；明确要求时可修改本地截止时间、状态或笔记，并通过对话撤销。
本地覆盖不会改变平台原始事实，也不会被下次同步清除。不能提交作业、发邮件或变更来源账号。
回答随接口输出流式显示，**Activity** 可展开查看工具执行与修改结果，**Stop** 停止继续生成。
按需链接正文只在当前请求内暂存，不整份入库；具体支持范围见 [Chat 工具](CHAT_TOOLS.md)。
读取结果有缺口时仍显示警告；不支持工具调用的兼容接口可以退回缓存证据回答，并提示限制。
当前已完成本地和模拟模型测试，**具体模型的回答质量与服务兼容性仍需单独验证**。

## 数据与恢复

默认相对于项目目录存储：

```text
data/
  coursedeck.sqlite3          # courses、tasks、local states、connector states、sync history
  knowledge.sqlite3           # 资料正文/证据、Chat 配置与对话历史；不含 API key
  browser-profiles/
    google_classroom/
    gradescope/
    webassign/
    brightspace/
    gmail/
  backups/
    <backup-id>/             # Settings → Backup & restore → Back up now
      coursedeck.sqlite3
      knowledge.sqlite3
      manifest.json          # 文件大小、SHA-256、schema 指纹和备份原因
  debug/                     # 仅手动 smoke 测试截图；应用不默认保存真实页面
```

可通过环境变量 `COURSEDECK_DATA_DIR` 指定绝对路径。OAuth 凭据在 OS vault 中，不在 SQLite
或普通 JSON 文件里。Windows 长 token 分段保存，全部片段就绪后才发布新版本，失败保留旧版本。
不支持明文 keyring fallback。WebAssign 浏览器模式也需要 OS vault 保存恢复会话所需的凭据。

在 **Settings → Backup & restore** 操作：

1. **Back up now** 生成一个包含双库和 `manifest.json` 的备份目录。
2. 在 Backup 选择记录，点击 **Check backup**；检查文件校验和、SQLite 完整性/外键以及 schema 兼容性，
   显示将替换的数据范围和不包含的登录会话/凭据。
3. 点击 **Restore this backup** 才执行。程序会再次检查，并先生成标记为 **Before restore** 的当前数据备份。
4. 恢复成功后重新读取快照；任务、资料、对话、变化日志和本地设置一起恢复，现有系统凭据与浏览器会话不被替换。

备份、导入和恢复期间进入维护状态，先等待正在处理的请求结束，再暂停来源、邮件及资料抓取，阻止其他数据访问。
心跳与恢复入口保持可用；长请求尚未结束或仍在专用窗口登录时，会提示稍后重试，不抢占登录窗口。
结束后恢复后台工作。双库按顺序通过 SQLite online backup API 复制；维护屏障提供稳定的数据对，
但磁盘写入不是跨文件原子事务。复制失败会尝试从 Before restore 备份回滚两个数据库。
若回滚也未完成或进程中断，恢复标记会保留，普通访问与后台同步继续受阻，需先恢复经过校验的备份。
在已有恢复标记时，不会把可能混合的当前数据库创建为新备份；再次恢复失败仍保留原标记和可用的原回滚备份。
只有目标双库恢复并通过检查后，才解除恢复状态。

旧版 `coursedeck-同一时间戳.sqlite3` / `knowledge-同一时间戳.sqlite3` 放在 backups 根目录时会显示 **Legacy**。
**Import backup** 检查成对文件并生成新格式清单，保留原文件，再允许恢复；导入本身不替换当前数据。
已知旧结构的配对备份会显示 **Upgrade needed**；点击 **Prepare upgraded copy** 生成升级后的副本，
原件保持不变，再对新副本执行检查和恢复。缺少配对数据库、损坏文件、未知结构或未来版本会明确拒绝。
不要混用不同时间的数据库，也不要把旧 `-wal` / `-shm` 文件混入备份。备份不包含 browser profiles 或系统凭据库。
更换 data 绝对路径时，OS vault 命名空间也会变化，需要重新授权。

任务详情中的 **Local edits** 可查看本地修改记录并 **Undo** 最近一项修改。
若任务在读取后又发生变化，撤销会停止；点击 **Refresh edits** 检查最新记录后再操作。

数据目录、profile、调试产物和常见凭据文件均已 gitignore。数据库和备份含个人学习内容，
请按私人文件管理；应用不会整体上传数据库。启用 Chat 后，相关上下文会按上文说明发送给模型接口。

## 同步语义

- 课程平台每轮完成后间隔 30 分钟自动同步；服务启动后首次定时同步等待 30 分钟。
  Sync on startup 只控制是否启动后立即同步，不关闭定时同步。Gmail 仍独立每 5 分钟检查。
- 启动、定时和手动课程同步共用串行队列，同一时间只抓取一个课程平台；重复的待执行请求合并，
  不积压多轮定时任务。单个平台超时或抓取失败后继续下一个，关闭服务时取消运行和排队任务。
- 网络失败和限流最多追加两次重试，等待约 15 秒、60 秒；仍使用同一串行队列，不无限重试。
  认证失效不自动反复登录；Sources 提供重连操作，并显示失败阶段、类别、已重试次数或下次重试时间。
  解析错误和覆盖缺口需核对来源，有限重试并不代表能恢复所有信息。
- 以转义后的 `provider:course:assignment` 身份 upsert。`first_seen_at` 保留，`last_seen_at` 更新。
- 本地隐藏、dismiss、pin、note、priority 存于独立表；绝不写回 LMS。
- `due_at` 与 `closes_at` 始终分开。无法解析新的时间时，保留此前已知时间并记录 warning。
- 来源确认已提交、已评分或已完成的任务进入 Done；本地勾选也进入 Done。
- 完整读取某门课程的任务列表后，未找到的旧任务显示 Not found in source；不会自动归档或当作完成。
  独立任务类型可以分别确认覆盖，例如 Brightspace 的作业提交区与课程内容。
- 本次未刷新到且不能确认消失的任务显示 Not refreshed；读到任务但状态不可确认时显示 Status unknown。
  这些任务保留并标灰；缓存的旧提交状态不会被当作本次确认。任务重新出现、状态可读后自动恢复，笔记不变。
- 不物理删除任务。本地隐藏仍可从 Hidden 查看和恢复。
- UI 每三秒向本地后端读取快照，不意味着每三秒访问 LMS；课程抓取由启动、定时或手动同步触发。
- 右上角独立检测本地服务心跳，超时显示 Disconnected，恢复后自动回到 Connected。
- 界面固定深色。侧栏 COURSES 的 + 可新建课程；Courses → Edit course 可添加多个来源课程。
- 一个来源可包含多门课程，同一本地课程可关联多个来源；每个来源课程只归属一个本地课程。
- Courses → Edit course → **Merge course** 可把当前课程合入指定课程。目标保留名称与颜色，
  来源关联和本地引用跟随合并；作业身份、内容、笔记和完成状态保持独立，不做跨来源任务去重。
- Edit course → **Color** 设置课程颜色，侧栏、任务和邮箱课程标识保持一致；Use default color 恢复默认。
- Inbox 菜单 → **Mail rules** 支持添加、编辑、启用、禁用或删除关键词规则；匹配可用于课程、类别、自动忽略或明确无分类。
  变更先预览后应用；规则无需 AI，禁用保留配置，手工分类和恢复操作仍可使用。
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
uv run python -m scripts.smoke_chat_ui
uv run python scripts/smoke_profiles.py
uv run python scripts/smoke_vault.py
```

`smoke_ui.py` 只操作合成的临时数据，验证课程绑定、笔记、心跳拒绝/超时/恢复、深色桌面与手机布局；
截图保存在 data/debug，不会修改当前工作区的真实数据库。
`smoke_chat_ui.py` 使用静态前端和本地模拟接口，验证无 AI 资料库、引文、对话错误恢复、持久警告、密钥控件及手机布局，
不会调用真实模型或使用学校账号。
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
