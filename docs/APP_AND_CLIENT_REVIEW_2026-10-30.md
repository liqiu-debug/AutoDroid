# 双端 App 与客户端改进审查（2026-10-30）

> 审查范围：`AutoDroid-Android`（AutoJs6 fork 的 AutoDroid 产品代码）、`AutoDroid-iOS`（SwiftUI 客户端）、
> `AutoDroid-WDA`（设备端执行器）、`AutoDroid-Desktop`（Electron 客户端 + 内置引擎），
> 以及它们与 `AutoDroid` 平台之间的契约。
>
> 与既有文档的关系：本文**只收录新增问题**。2026-10-20 的跨端排查（`APP_AUDIT_2026-10-20.md`）
> 已修复项、平台侧 `IMPROVEMENT_ROADMAP.md` 的 P0–P2 清单均不再重复。
> 每项均标注证据位置（文件:行），P0/P1 结论已逐条回读源码核验。

## 结论速览

| 端 | P0 | P1 | 主题 |
|---|---|---|---|
| 跨端 | 2 | 1 | iOS 签名 5 天后失效；WDA 执行器在局域网无鉴权 |
| 跨端契约 | 0 | 2 | 常量在 3–4 种语言里各写一份；无能力协商、版本判断写死 `== 1` |
| Android | 3 | 3 | 明文 HTTP 承载口令与 JWT；更新包无校验；报告 WebView 泄漏 |
| iOS | 0 | 4 | 3 秒轮询 + 无界本地历史；运行中 401 孤儿化；缺 `target_bundle_id`（"永久锁死"初判有误，已降为 P2） |
| WDA | 3 | 2 | 无执行超时/看门狗；任意 bundle 启动；调度日志无界 |
| 桌面客户端 | 2 | 2 | preload 桥未绑定页面来源；`install-update` 可执行任意下载物 |
| 交付工程化 | 1 | 3 | 四仓库测试门禁缺失；桌面引擎与平台后端重复维护 |

---

## 一、跨端 P0

### P0-A  iOS 真机链路 5 天后整体失效（时间敏感）

`AutoDroid-WDA/autodroid/out/WebDriverAgentRunner-Runner.app/embedded.mobileprovision`
的有效期是 **2026-11-04T09:08:39Z**（已用 `security cms -D` 核验）。到期后手机上 WDA 无法启动，
`AutoDroid-iOS` 的用例/场景/定时执行全链路立即失效，而 iOS 真机验收 Phase 0 尚未完成。

**建议**：重签并重装 WDA 与 AutoDroid 两个 App；把"描述文件到期日"变成发布流程里的显式检查项
（例如在 WDA 构建脚本里解析 `ExpirationDate`，剩余 <14 天即告警），而不是靠人记。

### P0-B  WDA 执行器在局域网无鉴权，且可任意启动 App

- 所有执行器端点都注册为 `.withoutSession`，即不要求 WebDriver 会话、也无任何凭证：
  `AutoDroid-WDA/WebDriverAgentLib/Commands/ADTAutoDroidCommands.m:18-26`（`/autodroid/status|run|stop|logs|report|schedules|notifications`）。
- 监听地址由 `USE_IP` 决定，而该环境变量只被读取、从未被本 fork 设置：
  `WebDriverAgentLib/Utilities/FBConfiguration.m:139-142` 返回 `nil` →
  `WebDriverAgentLib/Routing/FBTCPSocket.m:53` 走 `nw_listener_create_with_port`，**绑定全部网卡**。
- `target_bundle_id` 无白名单，直接进入 `primitives.activeBundleID` 并交给
  `launchApplicationWithBundleId:`（`WebDriverAgentLib/AutoDroid/ADTRunner.m:230-233`、`ADTPrimitives.m:877-917`）。

**后果**：同一 Wi-Fi 下任何主机（或手机上任一能访问 127.0.0.1:8100 的 App）都能下发一段
任意脚本、驱动被自动化手机的前台 UI、启动任意已安装 App（包括银行/2FA 类），并能通过
`POST /autodroid/schedules` 注入带任意 `fetch_url`+`token` 的定时任务。fork 自己的 README
甚至以 `curl http://<LAN-IP>:8100/status` 作为示例，说明这是当前的设计而非意外。

**建议**：默认 `USE_IP=127.0.0.1`（仅当确实需要"平台直连手机"时显式放开），并为 `/autodroid/*`
加一个每次安装生成的共享密钥请求头，在 `ADTAutoDroidCommands` 统一校验；同时按 bundle 里的
应用清单限制 `target_bundle_id`。注意：改成仅回环会影响平台侧 `selftest`/直连手机的路径，
需要一个显式的"局域网模式"开关而不是无条件放开。

---

## 二、Android 客户端

### P0-1  明文 HTTP 承载登录口令与 JWT（继承自上游，但产品化了风险）

`app/src/main/AndroidManifest.xml:216` 全局 `android:usesCleartextTraffic="true"`
（`git log -S` 显示来自上游提交 `3d6c2812`，非 fork 引入）；`net/ServerAddress.kt:7-15`
显式接受 `http://`，报错文案本身就在引导用户填 `http://192.168.1.10:8000`；
`net/AutoDroidApi.kt:146-151` 把口令 POST 到 `auth/token`，返回的 JWT 作为唯一凭证附加到所有请求。
401 时的 `clearLogin()` 是单向的（`:251-255`），被窃取的 token 在自然过期前一直有效。

**建议**：新增 `res/xml/network_security_config.xml`，把明文限定到"用户配置的那一个平台主机"
（或私有网段），不要再依赖全局开关；对非 TLS 主机在登录页给出明确风险提示。

### P0-2  内置更新会安装未经校验的 APK，且 JWT 出现在下载 URL 里

- `update/AppUpdateFlow.kt:39-41` 拼出 `.../packages/{id}/download?token=<jwt>`——token 进入
  平台/代理的访问日志（其余请求都走 `Authorization` 头）。
- `update/AppUpdateDownloader.kt:31-83` 流式下载后直接把 `FileProvider` URI 交给
  `PluginInstaller.installFromFileUri`（`AppUpdateFlow.kt:181`），**没有 size/sha256/包名/版本校验**
  （grep `sha|digest|verify` 无命中；`PackageItem.file_size` 仅用于展示）。
- 结合 P0-1 的明文传输，中间人或被攻陷的平台可以直接投递篡改后的 APK 并被静默安装。
- 代码里已经存在安全路径 `PluginInstaller.installFromUrlWithPrompt(url, expectedSha256)`，但未被使用。

补充说明（平台侧）：query-token 是平台**有意支持**的鉴权方式
（`backend/api/deps.py:112-122` 的 `get_current_user_allow_query_token`，
用于 WebSocket 握手与无法带头的下载端点，见 `backend/device_stream/router.py:357`、
`backend/api/packages.py:1002`）。所以这不是客户端自作主张，但也正因如此，
它是一条**平台级**的凭证外泄面：token 会进入反代/CDN/网关的访问日志。
本次只把它标在更新链路上，是因为那是唯一可以直接换成请求头的地方。

**建议**：更新下载改为带 `Authorization` 头并校验 sha256/包名/版本，更新通道强制 HTTPS；
平台侧对无法换头的端点（WS、Agent 脚本下载）至少在访问日志里对 `token` 查询参数做脱敏。

### P0-3  报告 WebView 泄漏 Activity，且未限制导航

`ui/ReportDetailActivity.kt:32-34` 打开 JS 与 DOM storage，`:56` 用
`webView.loadDataWithBaseURL(Session.serverUrl, html, ...)` 渲染远程报告 HTML；
该文件到 72 行结束，**没有 `onDestroy`**，也没有 `WebViewClient`。

**后果**：每次看报告后返回都会泄漏一个持 Context 的 WebView（会话内内存持续增长）；
报告 HTML 可在内嵌 WebView 中带 JS 任意跳转。

**建议**：补 `onDestroy { webView.stopLoading(); webView.removeAllViews(); webView.destroy() }`，
加 `WebViewClient` 拦截站外导航，显式 `allowFileAccess=false`/`allowContentAccess=false`。

### P1-4  加密存储失败时静默降级为明文 SharedPreferences

`data/Session.kt:38-49`：`EncryptedSharedPreferences.create` 抛任何异常即回退到
`app.getSharedPreferences(PREFS_NAME, MODE_PRIVATE)`，且 `:33` 的 `if (prefs != null) return`
把这次降级固化到整个进程生命周期——Keystore 损坏/备份恢复/部分 ROM 下，JWT 会以明文 XML 长期驻留。

**建议**：降级要留痕并告警（至少 Log + 设置页提示），并允许后续重试加密存储，而不是永久缓存明文实现。

### P1-5  定时任务失败被静默丢弃，单次任务还会永久丢失

`schedule/ScheduledRunService.kt:74,86,91,99` 处，未登录、脚本下发非 2xx、账号变化、
`main.js` 未就绪——四种情况都只 `Log.w(...)` 后 `return`：不重试、不落状态、不通知用户。
`schedule/ScheduleReceiver.kt:43-44` 在此之前就已把 `ONCE` 任务置为停用，
所以一次网络抖动就等于这个单次任务永久消失。

**建议**：对可恢复失败做有界退避重试并发失败通知；`ONCE` 任务在真正开始执行后再停用。

### P1-6  执行完成通知无视界面上的"通知"开关

`ui/RunActivity.kt:154-157` 无条件调用 `RunNotifier.watchResultAndNotify(...)`，
`run/RunNotifier.kt:61-109` 恒发系统通知；而用户看到的 `switch_notify`（`:62,148`）
只写进 `task_config.json` 的 `notify`，按 `run/RunBridge.kt:69-79` 的注释那是给 runtime.js 用的**飞书**开关。

**建议**：让系统通知受同一开关约束，或明确拆成"飞书通知"和"本机通知"两个控件。

### P2-7  目录筛选在 id 非数字时静默退化为"全部用例"

`ui/CaseListActivity.kt:157` 用 `node.id?.removePrefix("folder-")?.toIntOrNull()`，
`:192-194` 把解析结果（可能为 null）写入 `selectedFolderId`；而 `null` 同时是"全部用例"的哨兵值
（`AutoDroidApi.kt:164` 的 `folder_id: Int?`）。用户点了某个目录、界面也显示了目录名，
列表却是未过滤的全量数据。任何后端 id 方案变化都会触发。

**建议**：目录 id 保持字符串，或把"解析失败"当成错误处理，绝不与"不筛选"合并。

### P2-8  三个列表适配器重复实现状态徽标

`CaseListActivity.kt:269-286`、`ReportListActivity.kt:80-97`、`ScenarioListActivity.kt:108-125`
是三份拷贝的状态→颜色映射，且后两者都以 `R.layout.item_case` + 近乎相同的 `getView` 实现；
分歧已经出现（`RunNotifier.kt:74` 只统计 `"FAIL"`）。建议抽一个共享的状态徽标/行绑定工具。

---

## 三、iOS 客户端与 WDA 执行器

### P2-4  执行器断连期间的"待确认运行"文案误导，且手动解绑入口是死代码（已从 P0 降级）

`App/AppModel.swift:193-195` 在提交失败时留下 `finished=false` 且不清 `activeRunID`，
`hasPendingRun` 随即禁用所有运行按钮（`Views.swift:77,223`、`ScriptPreviewView.swift:73`），
`stop()` 因 `run_id` 不匹配抛错（`:307`），而唯一的手动解绑 `dismissUnknownRun()`（`:339-341`）
**在任何视图中都没有被调用**。

**但这不会永久锁死**（初稿判为 P0 是错的）：`AppModel.swift:244-251` 在执行器重新可达后，
发现 `status.run_id != activeRunID`，会向 WDA 查该 run 的报告，WDA 对未知 run_id 返回
`{found:false}`（`ADTAutoDroidCommands.m:75-80`，HTTP 200 非报错），客户端据此把运行标为
"执行器已切换任务，结果未知"并清空 `activeRunID`——也就是 **WDA 恢复后 3 秒内自愈**。
锁住的时段恰好等于 WDA 不可达的时段，而那段时间本来也跑不了。

真正的问题缩小为两点：断连期间用户看到的是"有尚未确认结束的运行，请先查看运行记录"
而不是"执行器未连接"，会让人去翻运行记录找一个并不存在的问题；以及一段永远不会执行的解绑代码。

**建议**：断连时把 `start()` 的拒绝文案改成指向执行器状态；把 `dismissUnknownRun` 接到运行详情页
作为兜底，或删掉它。

### P1-9  3 秒轮询 + 无界本地历史 + 每次全量重写

`App/AutoDroidApp.swift:13-16` 前台每 3 秒 `model.refresh()`；`AppModel.swift:419`
用 `runs.insert(record, at: 0)` 且**全仓无裁剪**；`:205`/`:430-431` 在每次状态变化、
每个轮询周期无条件重写 `runs.json` 与调度文件（`RunRecord` 含最多数千行 `logs` 与完整 `report`）；
`App/Storage.swift:28-31` 在 MainActor 上同步解码整个文件。

**后果**：运行数周后启动卡在大文件同步解码、每 3 秒重写数 MB、无谓唤醒与耗电。

**建议**：保留最近 N 条（如 50）并在写入时裁剪；无变化时跳过写盘；空闲时把轮询退避到 15–60 秒，
有活动运行时才用 3 秒。

### P1-10  WDA 无执行超时/看门狗，卡死脚本会永久占住执行器

`WebDriverAgentLib/AutoDroid/ADTRunner.m:295` 直接 `[context evaluateScript:mainSource]`，无截止时间；
`ADTPrimitives.m:181-187` 的 `jsSleep:` 用 `NSThread sleepForTimeInterval`（阻塞、不可中断），
停止标志 `__AUTODROID_SHOULD_STOP__`（`ADTRunner.m:244-245`）只在各原语边界被轮询。

**后果**：一个 `while(true){}`（仓库自带的 `autodroid/test_scheduler.py` 就在用忙循环）
会让 `running=YES` 永久保持；`POST /autodroid/stop` 置的标志永远不会被观察到，
在强制重启 WDA 之前不会再有任何运行或定时任务能被启动。

**建议**：给 JS 执行加可配置的最长墙钟时间（配合看门狗线程中断），并把 `jsSleep` 改为分片睡眠、
每片检查停止标志。

### P1-11  iOS 运行缺少 `target_bundle_id`，执行器的目标应用钉死形同虚设

`App/AppModel.swift:179-185`（运行）与 `:385-395`（定时）都没有发送 `target_bundle_id`，
而平台契约定义了该字段（`AutoDroid/backend/autojs_export/selftest.py:274`）、执行器也会读取
（`ADTRunner.m:230`）。脚本因此退化为"操作当前前台 App"，一次意外的前后台切换就可能作用到错误的 App。

**建议**：在两种载荷中都带上 bundle/清单里的目标 bundle id；若确实打算只靠 `start_app`，
就要在契约文档里写明，别让执行器侧保留一个永不生效的参数。

### P1-12  运行中 401 会登出并把在飞任务变成孤儿

`App/AppModel.swift:80-85` 对任何 401 调 `invalidateSession()`，后者清空 `activeRunID`
（`:119-123`）。token 在脚本执行期间过期，用户会被丢回登录页，而 WDA 仍在跑这个任务；
重新登录后可能重新接管或归错账号。建议运行中的 401 只标记"需重新登录后同步"，不清 `activeRunID`。

### P2-13  执行器只保留最近 10 次运行目录，与 App 的补传队列互相打架

`ADTRunner.m:549-554` 的 `pruneRunsRoot:keep:10`，而 App 的自动补传依赖
`executor.report(runID:)` 还能取回文件（`AppModel.swift:322-325`）。若 App 被锁屏/后台跨越
10 次运行且从未取回 `record.report`，结果会永久丢失且只提示"执行器仅保留最近十次运行文件"。
建议提高保留数量，或在 `refresh()` 里对每个已完成运行即时取回并持久化，不论上传是否成功。

### P2-14  调度状态持久化了完整事件日志，且每 3 秒被整包拉取

`ADTRunner.m:349,526` 把 `[self logsSince:0]` 存进事件（`:500` 上限 100 条），
`:424` 的 `scheduleStatus` 返回全部事件，`:428-434` 写盘；App 每 3 秒 GET `/autodroid/schedules`
（`AppModel.swift:228,404`）。该 JSON 可达数十 MB，并在每次 tick 重写。
建议只持久化/返回每条事件的最近几行日志，详细日志按需拉取。

### P2-15  iOS 侧其余待办（低优先，一并记录）

- `/autodroid/logs?since=N` 的游标语义与缓冲区截断（`ADTRunner.m:133-135` 丢最旧 2000 行 vs
  `ADTAutoDroidCommands.m:70-72` 用绝对下标）不一致——App 目前绕过它，但契约是错的。
- 图像句柄与元素句柄在单次运行内无上限（`ADTImageTools.m:279-288`、`ADTPrimitives.m:498-504`），
  图像匹配密集的脚本可能把 WDA 进程撑爆；建议 LRU 上限。
- `CompletionNotifications.sent`（`CompletionNotifications.swift:8,33`）与
  `AppModel.uploadAttempts`（`AppModel.swift:35`）都是无界增长的 UserDefaults 集合。
- iOS 测试只覆盖 `ClientTests`（客户端与地址校验），`AppModel` 的重传队列/调度读回/
  卡死恢复（P0-4、P1-12）这些高风险状态机没有测试；建议注入桩 `ExecutorClient`/`PlatformClient`
  与内存版 `LocalStorage` 后补单测。

---

## 四、桌面客户端（Electron）

### P0-5  preload 桥不绑定页面来源，任何导航后仍可调用特权 API

`main.js:110-114` 的 `contextIsolation/nodeIntegration/sandbox` 配置本身是正确的，
但 `main.js` 只设置了 `setWindowOpenHandler`（`:127-132`，只管新窗口），**没有 `will-navigate` 守卫**：
主窗口 webContents 允许自由导航，而 preload 桥对**每一个**载入该 webContents 的文档都生效。
一旦平台 UI 出现 XSS（或远程响应被中间人改写、或有跳转），攻击者页面就继承 `window.desktop`，
可以直接调 `getSavedLogin()`（明文口令）与 `install-update`（见 P0-6），一次导航即完成客户端沦陷。

**建议**：加 `webContents.on('will-navigate')` 白名单（仅本机引擎来源），并在各
`ipcMain.handle` 内校验 `event.senderFrame.url`。

### P0-6  `install-update` 会下载并执行渲染进程指定的任意二进制

`updater.js:222-224` 注册的 `desktop:install-update` 由渲染进程完全控制 `url` 与 `fileName`；
`updater.js:162-208` 的校验只有"URL 是 http(s)"+"文件名不含分隔符"，`version` 注释里明确写着
"壳不消费"（`:161`）；下载后在 Windows 上 `spawn(filePath, [], {detached:true})`（`:127`）
直接以用户权限运行——**没有签名/哈希校验、没有扩展名白名单、没有降级保护**。
即使修好 P0-5，任何 UI 层 XSS 或更新通道中间人都能升级为本地代码执行。

**建议**：要求校验制品哈希/签名，限制扩展名，比较版本并拒绝降级，并把下载源固定到受控主机。

### P1-13  本地引擎 API 无鉴权、无 Host/Origin 校验

`engine/app.py:210` 以 `app.include_router(local_api_router)` 挂载，
`engine/local_api.py` 暴露 `/api/cases/{id}/run`、`/api/device/interact`、
`/api/device/execute_step`、`/api/devices/{serial}/screenshot`、WS 触控/投流等设备操作，
全部无认证依赖，也不校验 `Host`/`Origin`（`engine/app.py:1084` 的注释直接写着"本地端点不鉴权"）。
引擎绑定 `127.0.0.1` + 随机端口（`engine/__main__.py:25-40`）——这抬高了门槛，
但**不能阻止 DNS rebinding**：`evil.com` 解析到 127.0.0.1 后，页面可以用简单请求/WebSocket
握手（不受 CORS 读限制、不触发预检）驱动 adb。

**建议**：每次启动生成随机 token，经 `additionalArguments`/preload 交给渲染进程，所有路由与 WS 握手校验；
同时强制 `Host: 127.0.0.1:<port>` 并拒绝非预期 `Origin`。

### P1-14  UI 无 CSP、无权限处理器

`engine/app.py:121-146` 的 `_serve_static` 只设 `Cache-Control`，没有 `Content-Security-Policy`、
`X-Content-Type-Options`、`X-Frame-Options`；`main.js` 也没有 `onHeadersReceived` 注入 CSP，
没有 `setPermissionRequestHandler`。这正是 P0-5/P0-6 链条所缺的那层纵深防御。
建议注入严格 CSP（`default-src 'self'`，脚本不用 `unsafe-inline`）并默认拒绝摄像头/麦克风/定位等权限。

### P2-16  其余可靠性与工程化项

- **引擎可能在退出时成为孤儿**：`main.js:607-613` 的 `will-quit` 调 `stopEngine()` 但不等待，
  而 `main.js:485-498` 的 `SIGKILL` 兜底挂在被 `unref()` 的 3 秒定时器上——进程可能先退出，
  留下仍占着 adb 与端口的引擎，下次启动再拉起第二个。
- **Windows 引擎会弹控制台窗口**：`engine.spec:68` 的 `console=True`。
- **更新包在磁盘上无限累积**：`updater.js:183-201` 只在失败时删除，成功安装后
  `userData/updates/` 里的安装包永不清理（每个数百 MB）。
- **桥接测试是源码字符串断言**：`tests/electron_bridge_check.js:372-379,458-463,547-567`
  大量 `mainSrc.includes('…')`，只要字样出现在注释里就能通过，无法发现行为回归——
  而它守护的恰恰是 P0-5/P0-6 这类安全接线。建议改为真实触发 `will-navigate`、
  用敌意 URL 调 IPC。
- **最高的三项风险没有回归测试**：`tests/` 中检索 `Host|origin|rebind|downgrade|checksum|signature`
  无命中，即 P0-5/P0-6/P1-13 均无覆盖。
- **未签名发布**：`package.json:68-125` 的 `win`/`mac` 段没有签名/公证配置；
  `build-windows.ps1:46-59` 下载 platform-tools 时也没有校验和。
- 少量死代码/重复：`engine/app.py:76-80` 的 `platform_name` 两个分支相同；
  frontend-dist 路径解析在 `main.js:315-317` 与 `engine/app.py:58-61` 各写一份。

---

## 五、交付工程化

### P0-7  四个仓库的测试都不在任何 CI 门禁内

| 仓库 | 现有 workflow | 测试是否被执行 |
|---|---|---|
| `AutoDroid-Android` | `android.yml`（push/PR 到 master） | 否——只 `assembleInrtRelease` / `assembleAppRelease`，从不跑 `:app:testAppDebugUnitTest` |
| `AutoDroid-iOS` | 无 | 否——**完全没有 CI**，`swift test`（25 项）无人守 |
| `AutoDroid-WDA` | 上游 Appium 的 5 个 workflow | 否——`npm test` 只跑 `build/test/unit/**/*.spec.js`；原生的 `ADTRunnerTests.m` / `ADTImageToolsTests.m`（在 `WebDriverAgentTests/UnitTests/`，需 `xcodebuild test`）没有任何 workflow 触达（grep `xcodebuild.*test` 无命中） |
| `AutoDroid-Desktop` | `build-windows.yml`（仅 tag / 手动） | 否——只出 Windows 安装包，node 与 pytest 测试套件从不运行 |

也就是说，10-20 那次跨端排查里手工跑的 Android 7 项、iOS 25 项、WDA 9 项测试，
和桌面端那份测试套件，目前都只在"人记得跑"的时候才跑。

**建议**：按平台侧 `ci.yml` 的形态各补一个 test job（Android 加
`./gradlew :app:testAppDebugUnitTest`；iOS 加 `swift test`；WDA 加原生 XCTest target 的
`xcodebuild test`；桌面端加 `npm run check` + `npm run test:engine`），先跑通再加分支保护。

### P1-15  桌面内置引擎与平台后端是同一批文件的两份拷贝

`AutoDroid-Desktop/engine/` 共 53 个 Python 文件、**18680 行**，其中 43 个与平台 `backend/` 对应，
**32 个能找到同名文件、31 个已经不同**——累计 358 行分歧（`device_stream/manager.py` 差 83 行、
`fastbot/adb.py` 差 46 行、`drivers/cross_platform_runner.py` 差 27 行）。
两仓最后一次提交仅相隔一天，说明这不是"陈年分叉"，而是**并发演进中的持续漂移**：
任何 `drivers/`、`device_stream/`、`step_contract`、`execution_errors` 的修复都必须人工搬两遍。
（抽检 `execution_errors.py` 的错误码集合目前一致，说明还没坏——但这是靠人同步维持的。）

这不是无意的拷贝，而是**有意的 vendor**：文件头就写着来源与剥离点，例如
`engine/core/step_contract.py:1` 的
`# 来源：AutoDroid backend/step_contract.py（vendor）。剥离点：仅改写导入 backend.utils.pydantic_compat → engine.core.utils.pydantic_compat。`
`engine/core/execution_errors.py:1` 则是"剥离点：无（纯逻辑模块）"——也就是说**整个文件与平台逐字相同**，
平台改了它，桌面端不会知道。

**建议**：把共享部分抽成一个可安装的内部包（或 git submodule/subtree + 版本钉死），
引擎只保留网关适配层（`local_api.py`、`proxy.py`、`ws_proxy.py`、`presence.py`、`server_address.py`
这几个确实是桌面独有的文件）；短期至少在 CI 里加一步"共享文件与平台版本 diff 超阈值即失败"的护栏。

### P1-16  平台依赖只有下限、没有锁定

`requirements-base.txt` 等全部使用 `>=` 下限（如 `uiautomator2>=3.5`、`pydantic>=2.12`、
`Pillow>=12.1`、`opencv-python>=4.10`），`frontend` 有 `package-lock.json` 但 Python 侧没有锁文件。
这意味着相隔数月的两次 `pip install -r requirements.txt` 可能解析出不同版本，
而 `pydantic`/`uiautomator2` 这类依赖的主版本变动会造成运行时行为差异。

**建议**：加一份 `constraints.txt`（或改用 uv/pip-tools 生成锁定文件），并在 CI 里校验可解析。

### P1-17  平台运行时有隐含的单进程假设，且无任何防护

`backend/scheduler_service.py:36-37` 在进程内 `BackgroundScheduler.start()`，
`execution_limiter`、中止注册表（`run_control.py`）、`drivers/driver_pool.py` 也都是进程内单例，
而 `scripts/start_lan.sh:56` 的 `uvicorn` 没有 `--workers`，代码里也没有 worker 数校验
（grep `workers|WEB_CONCURRENCY` 在 `main.py`/`core/`/`database.py` 均无命中）。
默认单 worker 是对的，但运维一旦为了吞吐加 `--workers 4`，会静默出现：定时任务重复触发、
并发计数失真、中止信号送不到持有者、投流广播分裂。另外 `main.py` 仍用已废弃的
`@app.on_event("startup")`（`:201`），FastAPI 新版本建议迁到 `lifespan`。

**建议**：启动时校验 worker 数（>1 就拒绝或至少大声告警），并把"必须单进程"写进部署文档；
顺手迁移到 `lifespan`。

---

## 六、跨端契约一致性

方法：以 `docs/APP_RUNNER_API.md` 为准，把后端实际返回的 schema 与三个客户端实际解析的字段逐项对照。
下面只列**已经漂移或缺少守卫**的地方（每条都已两侧回读）。

### P1-18  同一份常量在 3–4 种语言里各写一遍，且已经开始不一致

| 常量 | 声明处 |
|---|---|
| E2xxx 错误码表 | Python `backend/execution_errors.py:23-35`；JS `backend/autojs_export/runtime.js:24-34`（`ERR`）；ObjC `AutoDroid-WDA/.../ADTRunner.m:327`（字面量 `@"E2999_EXECUTION_ERROR"`） |
| 动作词表 + 中文名 | Python `backend/step_contract.py:30-60`；JS `runtime.js:865-867`（白名单）+ `:466-858`；Kotlin `AutoDroid-Android/.../ui/CaseDetailActivity.kt:116-130`；Swift `AutoDroid-iOS/Sources/Core/ScriptPreview.swift:18-21` |
| 执行状态词表 | 后端 `backend/api/autojs.py:548-558`（`PASS/FAIL/WARNING/ABORTED`+`RUNNING`）；Android `ReportListActivity.kt:87-92`（多了 `FAILED/ERROR`）；iOS `FeatureViews.swift:37,46`（只有 `PASS/FAIL/RUNNING`，无 `WARNING/ABORTED`） |

**后果**：改一处不改其余，症状是"静默失效"——比如在 `execution_errors.py` 里改一个错误码，
设备端仍上报旧码，后端的 `suggestion_for()` 匹配不上，用户恰好在这一批改动上丢掉"怎么修"的提示；
新增一个动作，Android 预览显示原始英文 token，设备端如果白名单没同步就直接 `P1002` 拒绝。
动作词表实际已漂移：Android 预览优先取 `args.package`（`CaseDetailActivity.kt:149-150`），
iOS 取 `bundle_id ?? package ?? app_key`（`ScriptPreview.swift:50`）。
iOS 的结果筛选还缺 `WARNING/ABORTED` 两个值，这类完成态只能显示成和失败一样。

**建议**：把动作表、错误码、状态词表收敛成单一源（后端一份定义，构建期生成 JS 常量与客户端资源，
或提供一个 `GET /app-runs/capabilities` 返回），并让 `runtime.js` 的白名单也由它驱动。

### P1-19  没有能力协商，版本判断是写死的 `== 1`，而且只有单向

- Android `run/RunBridge.kt:35`：`require(bundle.format_version == 1 && manifest.platform == "android")`。
- iOS `Sources/Core/Models.swift:145`：`guard bundle["format_version"] == .number(1) …`。
- WDA 定时器 `ADTRunner.m:516`：`![bundle[@"format_version"] isEqual:@1]`。
- 回传方向：`backend/api/autojs.py:129` 声明了 `format_version`，但**全后端没有任何读取**（只有写入方）。

**后果**：`SCRIPT_FORMAT_VERSION`（`generator.py:37`）一旦升到 2，所有已安装的 App 会以
"脚本包不是预检通过的 Android/iOS 格式"这种通用文案拒绝新包，且没有升级提示；反过来平台会照单
接收一个自称 `format_version: 2` 的报告，所以这个字段作为守卫是单向失效的。

**建议**：注册响应里增加 `supported_format_versions` / `min_client_version`，客户端按区间比较而非字面量；
回传侧校验 `format_version`。

### P2-20  文档与实现不符（会误导按文档实现的人）

- `APP_RUNNER_API.md:44` 写的是 `"generator": "autodroid-autojs6/1"`，实际发的是
  `packager.py:35` 的 `GENERATOR_NAME = "autodroid/1"`（`:200`、`:407` 使用）——按文档做
  客户端识别（比如区分 AutoJs6 包与 WDA 包）会对每一个包判断错误。
- `APP_RUNNER_API.md:140-150` 只记了执行器 4 个路由，实际注册了 9 个
  （`ADTAutoDroidCommands.m:18-26` 多出 `/report`、`/schedules` GET+POST、`/notifications` GET+POST）；
  文档里的 `target_bundle_id` 字段**iOS 客户端从未发送**。
- `APP_RUNNER_API.md:145` 的 `/autodroid/logs?since=N` 增量游标其实是坏的（见 P2-15 第一条），
  唯一消费者 iOS 已绕过它，但仍要求 `next` 字段存在才能解码。

### P2-21  同一端点的可空性两端不一致，iOS 更脆

`TestExecutionRead`（`backend/api/reports.py:39-54`）里 `scenario_name`/`start_time`/`status` 是必填、
且**根本没有 `report_id` 字段**；Android 把这三个都当 `String?`（`net/AutoDroidApi.kt:99-106`），
iOS 当成非可选 `String`（`Models.swift:37-42`）并额外解一个永远为 nil 的 `report_id`。
数据库侧 `start_time` 已在多处是 `Optional`（`reports.py:128,200`），一旦后端放松可空性，
iOS 整个报告列表会 `DecodingError` 白屏，Android 只是少显示一个字段。

### P2-22  其余小的语义分歧

- **`notify` 默认值**：WDA 兜底报告写 `request[@"notify"] ?: @YES`（`ADTRunner.m:333`），
  而 `runtime.js:1107,1227` 与后端 `autojs.py:128` 都是"默认不发"。这条兜底路径正是为
  "JS 异常、runtime 没来得及写报告"准备的——恰恰是用户关掉通知时最容易被绕过的一刻。
  iOS 目前总会带上 `notify`，所以是潜在而非现行问题。
- **WDA 里硬编码了客户端 bundle id**：`ADTRunner.m:366` 用
  `[bundleId isEqual:@"com.liuzhenyu.autodroid.ios"]` 做等值判断才把 AutoDroid 拉回前台。
  换个 bundle id 重签/白标/dev 构建后，执行完成回跳会静默失效，且没有任何报错。
- **`manifest.requirements` 平台发了、客户端没人读**：`packager.py:181-197,209` 输出
  `min_ios_version: 15` / `min_android_sdk: 24`，两端都没有校验；平台承诺的"iOS ≥ 15"
  在 iOS 14 上会变成运行期一个看不懂的 JS 错误，而不是启动前的明确拒绝。
- **分页上限各不相同**：Android 200/200/50（`AutoDroidApi.kt:162,171,184`），
  iOS 50/30（`Clients.swift:79,106`），后端默认 100（`cases.py:699`）；文档示例又是 100。
- **Android 的执行列表丢掉了文档宣传的筛选**：`APP_RUNNER_API.md:101` 承诺
  `status/keyword`，Android 只发 `skip/limit`（`AutoDroidApi.kt:181-185`），iOS 是发的。
- **本地通知行为两端不同**：Android 恒发且没有开关（`RunActivity.kt:156`），
  iOS 有 `systemNotify` 开关（`AppModel.swift:19,296`）——见 P1-6。
- **失败上报的重试语义两端不同**：iOS 有 60 秒退避重试（`AppModel.swift:260-272,314-338`），
  Android 完全没有（只提示"结果已保存在本机 report.json"）——即 10-20 审计里那条已知缺口，
  这里补充一个事实：**它是双端行为差异，不只是功能缺失**。

### 附：核对过、确认一致的部分

`auth/token` 的请求与响应形状；bundle 顶层键与 iOS 独有的 `adapter_js`；
`notify` 默认 `false`（除 WDA 兜底）；设备 serial 前缀规则（`autojs6:`/`ios:`）与注册/上报同一套推导；
重试上限 `0..3`；步骤状态词表 `PASS/FAIL/WARNING/SKIP`；`folders/tree` 的形状与字符串 id
（Android 有意不收 `all_cases`）。

---

## 附录 A：已核验的正向结论

- **无硬编码密钥**：五个仓库的已跟踪文件中，扫描 `sk-…` / `AKIA…` / `ghp_…` /
  `BEGIN … PRIVATE KEY` / 明文 `password="…"` 均无命中。
- **Android 未绕过 `JsonSafe`**：`org.autojs.autodroid` 里没有裸 Gson/`JObject` 取值，
  10 月那次 Gson null 崩溃的修复面是完整的；`Log` 中也没有输出 token/凭证。
- **Android 前台服务与 FileProvider 声明正确**：`ScheduledRunService` 带
  `foregroundServiceType="specialUse"` 及 API 34 所需的 `<property>`；
  FileProvider `authorities` 与下载器使用的一致。
- **桌面端 Electron 基线正确**：`contextIsolation:true`、`nodeIntegration:false`、`sandbox:true`；
  口令经 `safeStorage` 加密且文件 0600（`login_store.js:55,70-77`）；
  更新 URL 限定 http(s) 且拒绝路径穿越（`updater.js:32-46`）；
  静态服务有真实的路径包含校验（`app.py:126-130`）；引擎内无 `shell=True`。
- **桌面端仓库干净**：`release/`、`release-engine/`、`assets/platform-tools/` 均已 gitignore，
  无构建产物入库。

## 附录 B：建议排期

| 顺序 | 事项 | 理由 |
|---|---|---|
| 1 | P0-A 重签 WDA/App 描述文件 + 到期检查 | 5 天后 iOS 链路直接不可用，且有明确截止日 |
| 2 | P0-B WDA 执行器鉴权与绑定收敛 | 当前是"同一 Wi-Fi 即可接管手机"的口子 |
| 3 | P0-5 / P0-6 桌面端导航守卫 + 更新校验 | 一次导航/一次 XSS 即可本地执行任意代码 |
| 4 | P0-7 补四个仓库的测试门禁 | 后续所有修复的前提；成本低、收益长期 |
| 5 | P0-1 / P0-2 / P0-3 Android 明文、更新校验、WebView | 都属可被外部利用或稳定泄漏 |
| 6 | P1-15 桌面引擎去重 | 越晚做，需要人工同步的修复越多 |
| 7 | P1-18 / P1-19 常量单一源 + 能力协商 | 现在改一个动作/错误码要动 4 个仓；版本守卫是单向失效的 |
| 8 | 其余 P1 / P2 | 按迭代排入 |

> 说明：本文的 P0/P1 结论均已回读源码核验；P2 项来自同一轮代码走查，
> 未经真机复现。真机相关的验收边界（锁屏、省电、后台精确闹钟、WDA 长时间存活）
> 仍与 `APP_AUDIT_2026-10-20.md` 中记录的一致，未发生变化。

## 附录 C：按"小团队内部试用"阶段重排

上文按严重度排序，但项目目前处于小团队内部刚交付试用阶段，签名计划用付费开发者账号解决。
按这个阶段重排，清单实际分成两档——**关键区分不是"安不安全"，而是"与扩散有没有关系"**。

### 现在就做：与扩散无关、今天就会咬人，或者改动小到不值得等

| 项 | 为什么现在 | 改动量 |
|---|---|---|
| P0-A 描述文件 11-04 到期 | 不是安全项，是试用中断。付费开发者账号给的是 1 年期 profile，能彻底解决；账号没下来之前，用免费 Apple ID 的 7 天 profile 重签一次过渡，否则 11-04 之后 iOS 全停 | 账号 + 重签 |
| P0-7 四仓 CI 测试门禁 | 小团队靶心：测试靠人记得跑，最容易漏。这是整份清单里 ROI 最高的一项 | 每仓一个 job |
| P1-15 引擎 vendor 漂移护栏 | 平台改了 `drivers/`/`step_contract`、桌面没跟上——这是本周就可能发生的事，不是扩散后才有的 | 先加一步 CI diff 校验 |
| P0-3 Android 报告 WebView 泄漏 | 多看几份报告就能复现的内存增长 | `onDestroy` 三行 |
| P1-5 定时任务静默丢弃 | 用户设了定时任务没跑、还不知道没跑，试用阶段最伤信任 | 至少补一条失败通知 |
| P1-6 通知开关名不副实 | 关了"通知"还弹通知，试用期必被问 | 一处条件 |
| 桌面 `will-navigate` 白名单 + 更新扩展名/版本比较 | 各几行，顺手做；**签名/公证**留到对外发布 | 极小 |
| P2-20 文档与实现不符 | 新人按 `APP_RUNNER_API.md` 接入会踩 `generator` 与执行器路由两个坑 | 改文档 |

### 扩散前再做：真正的外向安全项

- **P0-B WDA 执行器鉴权/绑定收敛**——但有一个前提要先确认：平台是否需要从局域网**直连手机**
  （`selftest.py` 走的是手机 LAN IP）。如果日常只用 App 内的 `127.0.0.1:8100`，
  那 `USE_IP=127.0.0.1` 一行就能收掉整个口子，那就不必等；如果需要直连，才涉及共享密钥这类设计。
- **P0-1 明文 HTTP**——如果手机填的是 Cloudflare Tunnel 域名，这一段本来就是 TLS；
  只有直接填局域网 IP 的场景才是明文。所以它取决于团队实际怎么填地址。
- P0-2 更新包校验、P0-5/P0-6 的完整治理（签名校验、CSP、权限处理器）、P1-13 引擎本地 API 鉴权、
  P1-16 依赖锁、P1-18/P1-19 常量单一源与能力协商——这些都可以排到对外发布之前。

### 一个提醒

如果平台像目前这样挂在 Cloudflare Tunnel 后面对外可达，那"内部试用"并不等于"没有公网入口"。
路线图里已知的 P2.9（报告读取端点无鉴权）在这种部署下是真实可达的——本次顺手核验：
`backend/api/reports.py` 全文 **0 处** `get_current_user`，`main.py:110` 挂载时也不带 `dependencies`，
即执行列表、执行详情、HTML 报告下载（含失败截图）在 `/api/reports/*` 与旧别名 `/reports/*`
两个前缀下都可以无凭证读取。它不需要攀附 App，直接 URL 就能读，值得和 WDA 鉴权放在同一批里看。
