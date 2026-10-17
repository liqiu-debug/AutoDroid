"use strict";
/**
 * AutoDroid 设备端 Agent（AutoJs6 常驻脚本）
 *
 * 作用：让手机脱离 PC/USB 独立承接 AutoDroid 平台的执行任务。
 * 循环：注册/心跳 -> 领取任务 -> 下载脚本包 -> 执行 -> 结果回传（失败自动补报）。
 *
 * 使用方法：
 * 1. 将本脚本与 config.json 放在 AutoJs6 同一目录（参考 config.example.json）；
 * 2. 在 AutoJs6 中运行本脚本（建议加入「定时任务」并开启开机自启，
 *    或在打包应用中内置本脚本）；
 * 3. 前置条件：无障碍服务已开启；图像/OCR 步骤建议 Android 11+（无障碍截图）。
 *
 * 依赖：AutoJs6 6.x（http / zip / files / device / engines 模块）。
 */

var AGENT_VERSION = "1.0.0";


// ---------------------------------------------------------------------------
// 日志（同时输出到 console 与同目录 agent.log，便于无人值守排查）
// ---------------------------------------------------------------------------
function formatTime(d) {
    function pad(n) { return n < 10 ? "0" + n : "" + n; }
    return d.getFullYear() + "-" + pad(d.getMonth() + 1) + "-" + pad(d.getDate())
        + " " + pad(d.getHours()) + ":" + pad(d.getMinutes()) + ":" + pad(d.getSeconds());
}

function agentLog(level, message) {
    var line = "[" + formatTime(new Date()) + "][" + level + "] " + message;
    if (level === "ERROR") { console.error(line); }
    else if (level === "WARN") { console.warn(line); }
    else { console.log(line); }
    try {
        var logPath = files.join(files.cwd(), "agent.log");
        var logFile = new java.io.File(logPath);
        if (logFile.exists() && logFile.length() > 512 * 1024) {
            logFile.delete();
        }
        files.append(logPath, line + "\n");
    } catch (e) { /* 日志写入失败不影响主流程 */ }
}

function logInfo(message) { agentLog("INFO", message); }
function logWarn(message) { agentLog("WARN", message); }
function logError(message) { agentLog("ERROR", message); }

// ---------------------------------------------------------------------------
// 配置
// ---------------------------------------------------------------------------
function configPath() {
    return files.join(files.cwd(), "config.json");
}

function loadConfig() {
    var path = configPath();
    if (!files.exists(path)) {
        throw new Error("缺少配置文件: " + path + "（参考 config.example.json）");
    }
    var config = JSON.parse(files.read(path));
    if (!config.server || !config.token) {
        throw new Error("config.json 需要 server 与 token 字段");
    }
    config.poll_interval_seconds = Number(config.poll_interval_seconds || 30);
    return config;
}

function deviceIdentity() {
    var identity = "";
    try {
        if (device.getAndroidId) identity = String(device.getAndroidId() || "");
    } catch (e) { /* ignore */ }
    if (!identity) {
        try {
            var serial = String(device.serial || "");
            if (serial && serial !== "unknown") identity = serial;
        } catch (e2) { /* ignore */ }
    }
    if (!identity) {
        // 兜底：持久化一个随机 ID（写入 agent 目录）
        var idPath = files.join(files.cwd(), ".device_id");
        if (files.exists(idPath)) {
            identity = String(files.read(idPath)).replace(/\s+/g, "");
        } else {
            identity = "gen-" + String(new Date().getTime());
            files.write(idPath, identity);
        }
    }
    return identity;
}

function devicePayload(config) {
    var payload = {
        android_id: deviceIdentity(),
        name: config.name || "",
        serial: ""
    };
    try { payload.model = String(device.model || ""); } catch (e) { }
    try { payload.brand = String(device.brand || ""); } catch (e) { }
    try { payload.android_version = String(device.release || ""); } catch (e) { }
    try { payload.resolution = device.width + "x" + device.height; } catch (e) { }
    try { payload.serial = String(device.serial || ""); } catch (e) { }
    return payload;
}

// ---------------------------------------------------------------------------
// HTTP
// ---------------------------------------------------------------------------
function authHeaders(config) {
    return {
        "Authorization": "Bearer " + config.token,
        "Content-Type": "application/json"
    };
}

function httpJson(config, method, path, payload) {
    var url = config.server.replace(/\/+$/, "") + path;
    var options = { headers: authHeaders(config), contentType: "application/json", timeout: 15000 };
    var res = method === "GET" ? http.get(url, options) : http.post(url, payload, options);
    if (!res || res.statusCode < 200 || res.statusCode >= 300) {
        throw new Error("HTTP " + (res ? res.statusCode : "?") + " " + path);
    }
    return JSON.parse(res.body.string());
}

function loadModuleFile(fullPath) {
    // 优先内置加载器（new Function 实现 CommonJS，语义确定）；
    // 失败时回退 AutoJs6 的 require。
    if (!files.exists(fullPath)) {
        throw new Error("文件不存在: " + fullPath);
    }
    var source = files.read(fullPath);
    var moduleObj = { exports: {} };
    try {
        var factory = new Function("module", "exports", source);
        factory(moduleObj, moduleObj.exports);
        var hasKey = false;
        for (var key in moduleObj.exports) { hasKey = true; break; }
        if (moduleObj.exports && typeof moduleObj.exports === "object" && hasKey) {
            return moduleObj.exports;
        }
    } catch (e) {
        logWarn("[agent] 内置加载器失败，回退 require: " + (e.message || e));
    }
    if (typeof require === "function") {
        return require(fullPath);
    }
    throw new Error("模块加载失败: " + fullPath);
}

// ---------------------------------------------------------------------------
// 任务处理
// ---------------------------------------------------------------------------
function tasksDir() {
    var dir = files.join(files.cwd(), "tasks");
    if (!files.exists(dir)) files.ensureDir(files.join(dir, "placeholder"));
    return dir;
}

function pruneTaskDirs(keep) {
    try {
        var dir = tasksDir();
        var names = files.listDir(dir).sort();
        while (names.length > keep) {
            var oldest = names.shift();
            files.removeDir(files.join(dir, oldest));
        }
    } catch (e) {
        logWarn("清理历史任务目录失败: " + (e.message || e));
    }
}

function downloadPackage(config, task, taskDir) {
    var url = config.server.replace(/\/+$/, "") + task.package_url;
    var res = http.get(url, { headers: authHeaders(config), timeout: 60000 });
    if (!res || res.statusCode < 200 || res.statusCode >= 300) {
        throw new Error("下载脚本包失败: HTTP " + (res ? res.statusCode : "?"));
    }
    var zipPath = files.join(taskDir, "package.zip");
    files.writeBytes(zipPath, res.body.bytes());
    zip.unzip(zipPath, taskDir);
    files.remove(zipPath);
    files.remove(files.join(taskDir, "placeholder"));
    if (!files.exists(files.join(taskDir, "main.js"))) {
        throw new Error("脚本包结构异常: 缺少 main.js");
    }
}

function writeTaskConfig(config, task, taskDir) {
    files.write(
        files.join(taskDir, "task_config.json"),
        JSON.stringify({
            server: config.server,
            token: config.token,
            taskId: task.task_id
        })
    );
}

function retryUploadReport(config, taskDir) {
    var reportPath = files.join(taskDir, "report.json");
    if (!files.exists(reportPath)) return false;
    var payload = null;
    try {
        payload = JSON.parse(files.read(reportPath));
    } catch (e) {
        return false;
    }
    for (var attempt = 1; attempt <= 3; attempt++) {
        try {
            httpJson(config, "POST", "/api/device-runs/report", payload);
            logInfo("[agent] 结果补报成功（第 " + attempt + " 次）");
            return true;
        } catch (e) {
            logWarn("[agent] 结果补报失败（第 " + attempt + " 次）: " + (e.message || e));
            sleep(3000 * attempt);
        }
    }
    return false;
}

function appendReportLogs(taskDir) {
    // 把执行期日志（runtime 写在 report.json 里）同步到 agent.log，便于无人值守排查
    try {
        var reportPath = files.join(taskDir, "report.json");
        if (!files.exists(reportPath)) return;
        var payload = JSON.parse(files.read(reportPath));
        var lines = payload.logs || [];
        for (var i = 0; i < lines.length; i++) {
            logInfo("[run] " + lines[i]);
        }
        logInfo("[run] 执行结果: success=" + payload.success + " cases=" + (payload.cases ? payload.cases.length : 0));
    } catch (e) {
        logWarn("读取 report.json 日志失败: " + (e.message || e));
    }
}

function runTask(config, task) {
    var taskDir = files.join(tasksDir(), String(new Date().getTime()) + "_" + task.task_id);
    if (files.exists(taskDir)) files.removeDir(taskDir);
    files.ensureDir(files.join(taskDir, "placeholder"));

    logInfo("[agent] 领取任务 " + task.task_id + "（" + task.name + "）");
    logInfo("[agent] 下载脚本包: " + task.package_url);
    downloadPackage(config, task, taskDir);
    logInfo("[agent] 脚本包已就绪: " + taskDir);
    writeTaskConfig(config, task, taskDir);

    // 通过全局变量向 main.js 传递运行配置（同一引擎内加载，全局可见）
    global.__AUTODROID__ = {
        baseDir: taskDir,
        server: config.server,
        token: config.token,
        taskId: task.task_id
    };
    delete global.__AUTODROID_RESULT__;

    logInfo("[agent] 开始执行用例脚本");
    var execError = null;
    try {
        loadModuleFile(files.join(taskDir, "main.js"));
    } catch (e) {
        execError = e;
        logError("[agent] 脚本执行异常: " + (e.message || e) + "\n" + (e.stack || ""));
    }
    appendReportLogs(taskDir);

    var result = global.__AUTODROID_RESULT__ || null;
    var uploaded = !!(result && result.upload_ok);
    if (!uploaded) {
        logInfo("[agent] 尝试补报执行结果");
        uploaded = retryUploadReport(config, taskDir);
    }

    if (execError && !uploaded) {
        // 脚本启动前崩溃（未生成 report.json）：直接上报一条系统级失败
        try {
            httpJson(config, "POST", "/api/device-runs/report", {
                task_id: task.task_id,
                kind: task.kind,
                case_id: task.case_id,
                scenario_id: task.scenario_id,
                device: devicePayload(config),
                success: false,
                cases: [{
                    case_id: task.case_id,
                    success: false,
                    steps: [{
                        order: 0,
                        action: "system",
                        status: "FAIL",
                        error: "设备端脚本执行异常: " + (execError.message || execError),
                        error_code: "E2999_EXECUTION_ERROR"
                    }]
                }]
            });
            logInfo("[agent] 崩溃信息已上报");
            uploaded = true;
        } catch (e2) {
            logError("[agent] 崩溃上报失败: " + (e2.message || e2));
        }
    }

    if (uploaded) {
        logInfo("[agent] 结果已回传平台，清理任务目录");
        try {
            files.removeDir(taskDir);
        } catch (e3) {
            logWarn("任务目录清理失败（不影响执行）: " + (e3.message || e3));
        }
    } else {
        logWarn("[agent] 结果未能回传，保留目录待人工处理: " + taskDir);
    }
    return uploaded;
}

function ensureSingleInstance() {
    // 单实例保护：同一台手机重复启动 Agent 会导致任务被重复领取/执行。
    try {
        var enginesList = engines.all();
        var myId = String(engines.myEngine().getId());
        for (var i = 0; i < enginesList.length; i++) {
            var engine = enginesList[i];
            var engineId = String(engine.getId());
            if (engineId === myId) continue;
            var source = "";
            try {
                source = String(engine.getSource() || "");
            } catch (e) {
                source = "";
            }
            if (source.indexOf("autodroid_agent") >= 0) {
                logWarn("[agent] 检测到另一个 Agent 实例正在运行（engine " + engineId + "），本实例退出以避免任务重复领取");
                return false;
            }
        }
    } catch (e) {
        logWarn("[agent] 单实例检测失败（继续启动）: " + (e.message || e));
    }
    return true;
}

// ---------------------------------------------------------------------------
// 主循环
// ---------------------------------------------------------------------------
function main() {
    if (!ensureSingleInstance()) {
        exit();
        return;
    }
    var config = loadConfig();
    logInfo("[agent] AutoDroid 设备端 Agent v" + AGENT_VERSION + " 启动");
    logInfo("[agent] 平台: " + config.server + " / 设备名: " + (config.name || deviceIdentity()));

    var consecutiveErrors = 0;
    while (true) {
        try {
            var registered = httpJson(config, "POST", "/api/device-runs/register", devicePayload(config));
            var serial = registered.device_serial;
            consecutiveErrors = 0;

            var poll = httpJson(config, "GET", "/api/device-runs/pending?serial=" + encodeURIComponent(serial), null);
            if (poll && poll.task) {
                runTask(config, poll.task);
                pruneTaskDirs(5);
                continue; // 立即检查是否还有下一个任务
            }
        } catch (e) {
            consecutiveErrors += 1;
            logError("[agent] 轮询失败（连续 " + consecutiveErrors + " 次）: " + (e.message || e));
            if (consecutiveErrors >= 3) {
                logError("[agent] 请检查：平台地址/Token 是否有效、设备网络是否可达、服务端是否已更新");
            }
        }
        var waitSeconds = Math.min(
            config.poll_interval_seconds * (consecutiveErrors > 0 ? consecutiveErrors : 1),
            300
        );
        sleep(waitSeconds * 1000);
    }
}

main();
