"use strict";
/**
 * AutoDroid 运行时（脚本包内置，请勿手改）
 *
 * 由 AutoDroid 平台导出脚本包时随包分发，负责解释执行脚本步骤
 * （cases/*.js 里的 JSON 步骤清单），并把步骤级结果回传平台。
 *
 * 与 PC 执行链路（backend/drivers/android_driver.py + cross_platform_runner.py）
 * 保持语义一致：定位候选链、textContains 降级、错误码 E2xxx、
 * retry_count 重试、error_strategy（ABORT/CONTINUE/IGNORE）、
 * 图像多尺度匹配（0.8-1.2 + TM_CCOEFF_NORMED + SSIM 二次校验）、
 * OCR 提取规则（regex/boundary/preset）。
 *
 * 环境要求：AutoDroid Runner（Android 7.0+，无障碍服务已开启；图像/OCR 步骤
 * 建议 Android 11+ 走无障碍截图，无弹窗）。
 */

var FORMAT_VERSION = 1;

// ---------------------------------------------------------------------------
// 常量（与 PC 侧对齐）
// ---------------------------------------------------------------------------
var ERR = {
    E2001: "E2001_ELEMENT_NOT_FOUND",
    E2002: "E2002_WAIT_TIMEOUT",
    E2003: "E2003_ASSERT_TEXT_FAILED",
    E2004: "E2004_ASSERT_IMAGE_FAILED",
    E2005: "E2005_IMAGE_NOT_MATCHED",
    E2006: "E2006_OCR_NO_RESULT",
    E2007: "E2007_INPUT_FAILED",
    E2008: "E2008_APP_CONTROL_FAILED",
    E2009: "E2009_CLICK_NO_EFFECT",
    E2201: "E2201_INVALID_ARGS",
    E2999: "E2999_EXECUTION_ERROR",
    P1003: "P1003_SELECTOR_MISSING",
    P1002: "P1002_ACTION_NOT_SUPPORTED"
};

var TEMPLATE_SCALES = [1.0, 0.95, 1.05, 0.9, 1.1, 0.85, 1.15, 0.8, 1.2];
var IMAGE_MATCH_THRESHOLD = 0.9;       // click_image（PC _IMAGE_MATCH_THRESHOLD）
var IMAGE_CLICK_TIMEOUT_SECONDS = 5;   // PC _IMAGE_CLICK_TIMEOUT_SECONDS
var ASSERT_TEMPLATE_THRESHOLD = 0.95;  // PC _ASSERT_IMAGE_TEMPLATE_THRESHOLD
var ASSERT_SSIM_THRESHOLD = 0.9;       // PC _ASSERT_IMAGE_SSIM_THRESHOLD
var RETRY_BACKOFF_MS = 1000;           // PC RETRY_BACKOFF_SECONDS
var CLICK_TIMEOUT_SECONDS = 5;         // PC click/input 的 el.exists(timeout=5)
var CONFIRM_HINT_TOKENS = [
    "我知道了", "知道了", "确定", "确认", "允许", "同意", "好的",
    "ok", "yes", "allow", "confirm"
];

// ---------------------------------------------------------------------------
// 基础工具
// ---------------------------------------------------------------------------
function StepError(code, message) {
    this.name = "StepError";
    this.code = code;
    this.message = message;
}
StepError.prototype = new Error();
StepError.prototype.constructor = StepError;

function fail(code, message) {
    throw new StepError(code, message);
}

function isNullish(v) {
    return v === null || v === undefined;
}

function normalizeTextForMatch(value) {
    var text = String(isNullish(value) ? "" : value).toLowerCase();
    if (!text) return "";
    text = text.replace(/\s+/g, "");
    text = text.replace(/[^\w一-鿿]/g, "");
    return text;
}

function logLine(ctx, message) {
    var line = message;
    ctx.logLines.push(line);
    if (ctx.logLines.length > 2000) ctx.logLines.splice(0, 500);
    console.log("[AutoDroid] " + line);
}

function sleepMs(ms) {
    sleep(Math.max(0, Math.round(ms)));
}

// ---------------------------------------------------------------------------
// 截图（优先无障碍截图：Android 11+ 无弹窗；回退 MediaProjection）
//
// 重要：无障碍服务异常时 automator.captureScreen() 可能永久阻塞（等待一个
// 永不返回的 Promise），导致脚本卡死、报告不产出、后续执行全部"假死"。
// 因此截图统一放到子线程执行并加超时保护，超时即失败而不是挂起。
// ---------------------------------------------------------------------------
var SCREENSHOT_TIMEOUT_MS = 15000;

function captureScreenInner(ctx) {
    var img = null;
    try {
        if (typeof automator !== "undefined" && automator.captureScreen) {
            img = automator.captureScreen();
        }
    } catch (e) {
        img = null;
    }
    if (!img) {
        try {
            img = images.captureScreen();
        } catch (e2) {
            try {
                images.requestScreenCapture(false);
                img = images.captureScreen();
            } catch (e3) {
                img = null;
            }
        }
    }
    return img;
}

function captureScreen(ctx) {
    var holder = { img: null, error: null, done: false };
    var worker = threads.start(function () {
        try {
            holder.img = captureScreenInner(ctx);
        } catch (e) {
            holder.error = e;
        }
        holder.done = true;
    });
    var deadline = Date.now() + SCREENSHOT_TIMEOUT_MS;
    while (!holder.done && Date.now() < deadline) {
        sleepMs(100);
    }
    if (!holder.done) {
        try { worker.interrupt(); } catch (e) { }
        fail(ERR.E2999, "截图超时（" + (SCREENSHOT_TIMEOUT_MS / 1000) + " 秒）：无障碍截图/投屏权限疑似异常，请在系统设置中检查并重新开启无障碍服务");
    }
    if (holder.error) {
        throw holder.error;
    }
    if (!holder.img) {
        fail(ERR.E2999, "截图失败：无障碍截图不可用（需 Android 11+），且未授予投屏权限");
    }
    return holder.img;
}

// Native iOS image handles need explicit lifetime management. Android owns its
// own image wrappers and does not expose these optional scope hooks.
function withImageScope(work) {
    var scope = images.__beginScope ? images.__beginScope() : null;
    try { return work(); }
    finally { if (scope !== null) images.__endScope(scope); }
}

function screenshotBase64Jpeg(ctx) {
    try {
        return withImageScope(function () { return images.toBase64(captureScreen(ctx), "jpeg", 60); });
    } catch (e) {
        return null;
    }
}

// ---------------------------------------------------------------------------
// 定位（复刻 PC 侧候选链：逐个候选尝试；text 先精确 2s 再 textContains）
// ---------------------------------------------------------------------------
function buildSelector(by, selector) {
    switch (by) {
        case "id":
        case "resourceid":
        case "resource_id":
            return id(selector);
        case "text":
        case "label":
        case "name":
            return text(selector);
        case "description":
        case "desc":
        case "content-desc":
            return desc(selector);
        default:
            return null;
    }
}

function isTextLike(by) {
    return by === "text" || by === "label" || by === "name";
}

function isAccessibilityError(error) {
    var text = String((error && (error.message || error)) || "");
    return text.indexOf("无障碍") >= 0 || text.indexOf("accessibility") >= 0
        || text.indexOf("Accessibility") >= 0;
}

function findElement(ctx, locators, timeoutSeconds, action) {
    if (!locators || !locators.length) {
        fail(ERR.P1003, action + " 步骤缺少可用定位器");
    }
    var deadline = Date.now() + Math.max(1, timeoutSeconds) * 1000;
    var lastError = null;
    for (var i = 0; i < locators.length; i++) {
        var by = String(locators[i].by || "").toLowerCase();
        var selector = String(locators[i].selector || "");
        if (!selector) continue;
        var remain = Math.max(1, Math.ceil((deadline - Date.now()) / 1000));
        try {
            var s = buildSelector(by, selector);
            if (!s) {
                lastError = new StepError(ERR.P1002, action + " 不支持定位方式: " + by);
                continue;
            }
            var el = null;
            if (isTextLike(by)) {
                el = s.findOne(Math.min(2, remain) * 1000); // findOne 单位为毫秒
                if (!el) {
                    el = textContains(selector).findOne(Math.max(1, remain - 2) * 1000);
                }
            } else {
                el = s.findOne(remain * 1000);
            }
            if (el) {
                return { el: el, by: by, selector: selector };
            }
        } catch (e) {
            // 无障碍服务不可用等环境错误直接上抛，避免被掩盖为"元素未找到"
            if (isAccessibilityError(e)) {
                throw new StepError(
                    ERR.E2999,
                    "无障碍服务不可用: " + String(e.message || e)
                        + "（请在系统设置中重新开启 AutoDroid 的无障碍服务；ColorOS/OPPO 等机型开启后建议重启一次设备）"
                );
            }
            lastError = e;
        }
    }
    if (lastError && lastError instanceof StepError && lastError.code === ERR.P1002) {
        throw lastError;
    }
    fail(ERR.E2001, action + " 元素未找到: " + JSON.stringify(locators));
}

function pageSignature() {
    // 供 click_image 的"点了没反应"检测使用（镜像 PC _capture_page_signature_quick）
    try {
        // 注意：textMatches 必须传字符串正则（运行引擎的 selector 反射层不接受 JS RegExp 对象）
        var nodes = textMatches(".*").find();
        var parts = [];
        var limit = Math.min(nodes.length, 400);
        for (var i = 0; i < limit; i++) {
            var node = nodes[i];
            var text = "";
            try { text = String(node.text() || node.desc() || ""); } catch (e) { text = ""; }
            var bounds = "";
            try {
                var b = node.bounds();
                bounds = b.left + "," + b.top + "," + b.right + "," + b.bottom;
            } catch (e2) { bounds = ""; }
            parts.push(text + "@" + bounds);
        }
        return String(new java.lang.String(parts.join("|")).hashCode());
    } catch (e) {
        return "";
    }
}

// ---------------------------------------------------------------------------
// 图像匹配（复刻 PC find_template_match：多尺度 + TM_CCOEFF_NORMED + 可选 SSIM）
// ---------------------------------------------------------------------------
function matchTemplateMultiScale(shot, template, threshold, ssimThreshold) {
    try {
        return withImageScope(function () { return matchTemplateMultiScaleInner(shot, template, threshold, ssimThreshold); });
    } finally {
        if (images.__endScope) images.release(shot);
    }
}

function matchTemplateMultiScaleInner(shot, template, threshold, ssimThreshold) {
    var shotGray = images.grayscale(shot);
    var tplGray = images.grayscale(template);
    var best = null;
    for (var i = 0; i < TEMPLATE_SCALES.length; i++) {
        var scale = TEMPLATE_SCALES[i];
        var scaled = scale === 1.0
            ? tplGray
            : images.scale(tplGray, scale, scale);
        var tw = scaled.width;
        var th = scaled.height;
        if (tw > shotGray.width || th > shotGray.height) continue;
        var result = images.matchTemplate(shotGray, scaled, { threshold: threshold, max: 5 });
        var match = result.best();
        if (match && (!best || match.similarity > best.similarity)) {
            best = {
                similarity: match.similarity,
                point: match.point,
                width: tw,
                height: th,
                template: scaled
            };
        }
    }
    if (!best || best.similarity < threshold) {
        return null;
    }
    var ssimScore = null;
    if (!isNullish(ssimThreshold)) {
        try {
            var crop = images.clip(
                shotGray,
                Math.round(best.point.x),
                Math.round(best.point.y),
                best.width,
                best.height
            );
            ssimScore = images.ssim(crop, best.template);
            if (ssimScore < ssimThreshold) {
                return null;
            }
        } catch (e) {
            ssimScore = null; // SSIM 不可用时仅以模板阈值判定
        }
    }
    return {
        similarity: best.similarity,
        ssim: ssimScore,
        // Auto.js 匹配点为左上角，点击取中心（与 PC 返回中心点对齐）
        x: best.point.x + best.width / 2,
        y: best.point.y + best.height / 2
    };
}

function loadTemplateAsset(ctx, assetName) {
    if (!assetName) {
        fail(ERR.E2201, "缺少模板图引用");
    }
    // 单文件模式：模板图以 base64 内嵌，首次使用时落盘到缓存目录
    if (ctx.embeddedAssets && ctx.embeddedAssets[assetName]) {
        var cacheDir = files.join(files.getSdcardPath(), "autodroid_cache");
        var cachePath = files.join(cacheDir, assetName);
        if (!files.exists(cachePath)) {
            try {
                files.ensureDir(cachePath);
                var bytes = android.util.Base64.decode(ctx.embeddedAssets[assetName], android.util.Base64.DEFAULT);
                files.writeBytes(cachePath, bytes);
            } catch (e) {
                fail(ERR.E2005, "内嵌模板图落盘失败: " + assetName + " (" + (e.message || e) + ")");
            }
        }
        var cached = images.read(cachePath);
        if (!cached) {
            fail(ERR.E2005, "内嵌模板图解码失败: " + assetName);
        }
        return cached;
    }
    var path = files.join(ctx.baseDir, "assets", assetName);
    if (!files.exists(path)) {
        fail(ERR.E2005, "模板图文件不存在: " + path);
    }
    var img = images.read(path);
    if (!img) {
        fail(ERR.E2005, "模板图解码失败: " + path);
    }
    return img;
}

function ocrTemplateHint(ctx, assetName, templateImg) {
    // 镜像 PC _template_confirm_text_hint：OCR 模板图，命中确认类词条则返回提示文本
    if (ctx.hintCache[assetName] !== undefined) {
        return ctx.hintCache[assetName];
    }
    var hint = "";
    try {
        var texts = ocr.recognizeText(templateImg);
        var joined = normalizeTextForMatch((texts || []).join(""));
        if (joined) {
            for (var i = 0; i < CONFIRM_HINT_TOKENS.length; i++) {
                var token = CONFIRM_HINT_TOKENS[i];
                var normalizedToken = normalizeTextForMatch(token);
                if (normalizedToken && joined.indexOf(normalizedToken) >= 0) {
                    hint = token;
                    break;
                }
            }
        }
    } catch (e) {
        hint = "";
    }
    ctx.hintCache[assetName] = hint;
    return hint;
}

// ---------------------------------------------------------------------------
// OCR 提取规则（镜像 PC _apply_extract_rule）
// ---------------------------------------------------------------------------
function applyExtractRule(rawText, rule) {
    rule = rule || {};
    var ruleType = String(rule.extract_rule || "preset").toLowerCase();

    if (ruleType === "regex") {
        var pattern = rule.custom_regex;
        if (!pattern) fail(ERR.E2201, "extract_rule=regex 时必须提供 custom_regex");
        var regex = null;
        try {
            regex = new RegExp(String(pattern), "s");
        } catch (e) {
            regex = new RegExp(String(pattern));
        }
        var match = regex.exec(rawText);
        if (!match) {
            fail(ERR.E2999, "正则未匹配到内容: " + pattern);
        }
        for (var i = 1; i < match.length; i++) {
            if (!isNullish(match[i])) {
                return String(match[i]).replace(/^\s+|\s+$/g, "");
            }
        }
        return String(match[0]).replace(/^\s+|\s+$/g, "");
    }

    if (ruleType === "boundary") {
        var left = String(rule.left_bound || "");
        var right = String(rule.right_bound || "");
        var start = 0;
        if (left) {
            var leftIndex = rawText.indexOf(left);
            if (leftIndex < 0) fail(ERR.E2999, "未找到左边界: " + left);
            start = leftIndex + left.length;
        }
        var end = rawText.length;
        if (right) {
            end = rawText.indexOf(right, start);
            if (end < 0) fail(ERR.E2999, "未找到右边界: " + right);
        }
        var text = rawText.substring(start, end).replace(/^\s+|\s+$/g, "");
        if (!text) fail(ERR.E2999, "边界提取结果为空");
        return text;
    }

    var preset = String(rule.preset_type || "number_only").toLowerCase();
    var presetMatch = null;
    if (preset === "number_only") {
        presetMatch = /\d+(?:\.\d+)?/.exec(rawText);
    } else if (preset === "price") {
        presetMatch = /(?:¥|￥|\$)?\s*\d+(?:\.\d{1,2})?/.exec(rawText);
    } else if (preset === "alphanumeric") {
        presetMatch = /[A-Za-z0-9]+/.exec(rawText);
    } else if (preset === "chinese") {
        presetMatch = /[一-鿿]+/.exec(rawText);
    } else {
        fail(ERR.E2201, "不支持的 preset_type: " + preset);
    }
    if (!presetMatch) {
        fail(ERR.E2999, "内置模板未匹配到内容: " + preset);
    }
    var value = String(presetMatch[0]).replace(/^\s+|\s+$/g, "");
    if (preset === "price") {
        value = value.replace(/[¥￥$\s]/g, "");
    }
    return value;
}

// ---------------------------------------------------------------------------
// 动作实现
// ---------------------------------------------------------------------------
var ACTIONS = {};

ACTIONS.click = function (ctx, step) {
    var found = findElement(ctx, step.locators, CLICK_TIMEOUT_SECONDS, "click");
    if (!found.el.click()) {
        fail(ERR.E2001, "click 点击返回失败: " + found.selector);
    }
};

ACTIONS.input = function (ctx, step) {
    var text = String(step.args.text === undefined || step.args.text === null ? "" : step.args.text);
    var locators = step.locators || [];
    var errors = [];

    if (locators.length) {
        var found = findElement(ctx, locators, CLICK_TIMEOUT_SECONDS, "input");
        var el = found.el;
        // 策略 1：元素 setText（无障碍 ACTION_SET_TEXT）
        try {
            if (el.setText(text)) {
                return;
            }
            errors.push("setText 返回 false");
        } catch (e) {
            errors.push("setText: " + e.message);
        }
        // 策略 2：聚焦后 automator.input（无障碍 appendText，Android 11+）
        try {
            try { el.click(); } catch (e2) { /* 部分控件点击失败不阻断 */ }
            sleepMs(300);
            if (automator.input(text)) {
                return;
            }
            errors.push("automator.input 返回 false");
        } catch (e3) {
            errors.push("automator.input: " + e3.message);
        }
        fail(ERR.E2007, "输入失败（" + errors.join("; ") + "）");
    }

    // 无定位器：向当前焦点输入（镜像 PC input_focused）
    try {
        if (automator.input(text)) {
            return;
        }
        errors.push("automator.input 返回 false");
    } catch (e4) {
        errors.push("automator.input: " + e4.message);
    }
    try {
        var focused = className("android.widget.EditText").findOnce();
        if (focused && focused.setText(text)) {
            return;
        }
        errors.push("焦点控件 setText 失败");
    } catch (e5) {
        errors.push("焦点控件 setText: " + e5.message);
    }
    fail(ERR.E2007, "焦点输入失败（" + errors.join("; ") + "）");
};

ACTIONS.wait_until_exists = function (ctx, step) {
    findElement(ctx, step.locators, step.timeout || 10, "wait_until_exists");
};

ACTIONS.assert_text = function (ctx, step) {
    var expected = String(step.args.expected_text || "");
    var mode = String(step.args.match_mode || "contains").toLowerCase();
    if (!expected.replace(/^\s+|\s+$/g, "")) {
        fail(ERR.E2201, "assert_text expected_text 不能为空");
    }
    var found = textContains(expected).findOnce() || descContains(expected).findOnce();
    var matched = !!found;
    if (mode === "not_contains") {
        if (matched) {
            fail(ERR.E2003, "断言失败: 期望页面不包含 " + JSON.stringify(expected) + "，实际命中");
        }
        return;
    }
    if (!matched) {
        fail(ERR.E2003, "断言失败: 期望页面包含 " + JSON.stringify(expected) + "，未找到");
    }
};

ACTIONS.assert_image = function (ctx, step) {
    var mode = String(step.args.match_mode || "exists").toLowerCase();
    var template = loadTemplateAsset(ctx, step.args.image);
    var shot = captureScreen(ctx);
    var match = matchTemplateMultiScale(shot, template, ASSERT_TEMPLATE_THRESHOLD, ASSERT_SSIM_THRESHOLD);

    if (mode === "not_exists") {
        if (match) {
            // 二次确认，降低误报（镜像 PC 双重校验）
            sleepMs(200);
            var again = matchTemplateMultiScale(captureScreen(ctx), template, ASSERT_TEMPLATE_THRESHOLD, ASSERT_SSIM_THRESHOLD);
            if (again) {
                fail(ERR.E2004, "断言失败: 期望页面不存在图像 " + step.args.image + "，但匹配到 (similarity=" + again.similarity.toFixed(4) + ")");
            }
        }
        return;
    }
    if (!match) {
        fail(ERR.E2004, "断言失败: 期望页面存在图像 " + step.args.image + "，但未匹配到");
    }
    ctx.lastImageMatch = match;
};

ACTIONS.click_image = function (ctx, step) {
    var startedAt = Date.now();
    var assetName = step.args.image;
    var template = loadTemplateAsset(ctx, assetName);
    var hint = "";

    // 策略 1：模板图 OCR 文本提示（镜像 PC text-hint）
    try {
        hint = ocrTemplateHint(ctx, assetName, template);
    } catch (e) {
        hint = "";
    }
    if (hint) {
        try {
            var el = text(hint).findOne(2000) || textContains(hint).findOne(1000);
            if (el && el.click()) {
                logLine(ctx, "click_image 文本提示命中: " + hint);
                return;
            }
        } catch (e2) {
            // 回退模板匹配
        }
    }

    // 策略 2：多尺度模板匹配（镜像 PC 重试窗口 IMAGE_CLICK_TIMEOUT_SECONDS）
    var deadline = Date.now() + IMAGE_CLICK_TIMEOUT_SECONDS * 1000;
    var match = null;
    do {
        match = matchTemplateMultiScale(captureScreen(ctx), template, IMAGE_MATCH_THRESHOLD, null);
        if (match) break;
        sleepMs(500);
    } while (Date.now() < deadline);

    if (!match) {
        fail(ERR.E2005, "图像模板匹配失败: 未在屏幕上找到匹配的图像区域 (" + assetName + ")");
    }
    var matchElapsed = (Date.now() - startedAt) / 1000;

    var before = pageSignature();
    if (!click(Math.round(match.x), Math.round(match.y))) {
        fail(ERR.E2009, "图像已匹配，但点击返回失败");
    }
    sleepMs(120);
    var after = pageSignature();

    // 匹配耗时很长且点击后页面无变化时，同位重试一次（镜像 PC tap-no-effect 检测）
    if (before && after && before === after && matchElapsed >= 3.0) {
        if (!click(Math.round(match.x), Math.round(match.y))) {
            fail(ERR.E2009, "图像已匹配，但重试点击返回失败");
        }
        sleepMs(120);
        var retry = pageSignature();
        if (retry && retry === before) {
            fail(ERR.E2009, "图像模板匹配点击后页面未变化（tap-no-effect）");
        }
    }
};

ACTIONS.extract_by_ocr = function (ctx, step) {
    var region = step.args.region;
    if (isNullish(region)) {
        fail(ERR.E2201, "extract_by_ocr 需要 region");
    }
    var x1 = Number(region[0]);
    var y1 = Number(region[1]);
    var x2 = Number(region[2]);
    var y2 = Number(region[3]);
    if (isNaN(x1) || isNaN(y1) || isNaN(x2) || isNaN(y2)) {
        fail(ERR.E2201, "extract_by_ocr region 非法: " + JSON.stringify(region));
    }

    var attempts = Math.max(1, Math.floor((step.timeout || 10) / 1.5));
    var rawText = "";
    for (var attempt = 0; attempt < attempts; attempt++) {
        var shot = captureScreen(ctx);
        var width = shot.width;
        var height = shot.height;
        var rx1, ry1, rx2, ry2;
        if (x2 <= 1 && y2 <= 1) {
            rx1 = Math.round(x1 * width);
            ry1 = Math.round(y1 * height);
            rx2 = Math.round(x2 * width);
            ry2 = Math.round(y2 * height);
        } else {
            rx1 = Math.round(x1);
            ry1 = Math.round(y1);
            rx2 = Math.round(x2);
            ry2 = Math.round(y2);
        }
        rx1 = Math.max(0, Math.min(rx1, width));
        ry1 = Math.max(0, Math.min(ry1, height));
        rx2 = Math.max(0, Math.min(rx2, width));
        ry2 = Math.max(0, Math.min(ry2, height));
        if (rx2 <= rx1 || ry2 <= ry1) {
            fail(ERR.E2201, "extract_by_ocr 区域无效: [" + rx1 + "," + ry1 + "," + rx2 + "," + ry2 + "]");
        }
        var crop = images.clip(shot, rx1, ry1, rx2 - rx1, ry2 - ry1);
        var texts = ocr.recognizeText(crop);
        if (images.__endScope) { images.release(crop); images.release(shot); }
        var lines = [];
        for (var i = 0; i < (texts || []).length; i++) {
            var line = String(texts[i] || "").replace(/^\s+|\s+$/g, "");
            if (line && lines.indexOf(line) < 0) lines.push(line);
        }
        rawText = lines.join("\n");
        if (rawText) break;
        sleepMs(1000);
    }
    if (!rawText) {
        fail(ERR.E2006, "extract_by_ocr 未识别到文本（region=" + JSON.stringify(region) + "）");
    }
    var extracted = applyExtractRule(rawText, step.args.extract_rule || {});
    var outputVar = step.args.output_var;
    if (outputVar) {
        ctx.exportedVariables[String(outputVar)] = extracted;
    }
    return { output: { ocr_value: extracted } };
};

ACTIONS.sleep = function (ctx, step) {
    sleepMs(Number(step.args.seconds || 0) * 1000);
};

ACTIONS.swipe = function (ctx, step) {
    var direction = String(step.args.direction || "up").toLowerCase();
    if (["up", "down", "left", "right"].indexOf(direction) < 0) {
        fail(ERR.E2201, "不支持的滑动方向: " + direction);
    }
    // 镜像 uiautomator2 swipe_ext(direction, scale=0.8)：轴向上 10% <-> 90%
    var w = device.width;
    var h = device.height;
    var x1, y1, x2, y2;
    if (direction === "up") {
        x1 = w * 0.5; y1 = h * 0.9; x2 = w * 0.5; y2 = h * 0.1;
    } else if (direction === "down") {
        x1 = w * 0.5; y1 = h * 0.1; x2 = w * 0.5; y2 = h * 0.9;
    } else if (direction === "left") {
        x1 = w * 0.9; y1 = h * 0.5; x2 = w * 0.1; y2 = h * 0.5;
    } else {
        x1 = w * 0.1; y1 = h * 0.5; x2 = w * 0.9; y2 = h * 0.5;
    }
    if (!swipe(x1, y1, x2, y2, 300)) { fail(ERR.E2999, "滑动返回失败"); }
};

ACTIONS.back = function () {
    if (!back()) { fail(ERR.E2999, "返回操作失败，未检测到页面变化"); }
};

ACTIONS.home = function () {
    if (!home()) { fail(ERR.E2999, "返回桌面失败"); }
};

ACTIONS.start_app = function (ctx, step) {
    var isIOS = String(ctx.platform || "").toLowerCase() === "ios";
    var pkg = isIOS ? (step.args.bundle_id || step.args.app_id || step.args.package) : (step.args.package || step.args.app_id);
    if (!pkg) {
        fail(ERR.E2008, "start_app 缺少可用的应用标识（app_key 未映射）: " + (step.args.app_key || ""));
    }
    if (!app.launchPackage(pkg)) {
        fail(ERR.E2008, "启动应用失败: " + pkg);
    }
    var deadline = Date.now() + 5000;
    var sawForeground = false;
    while (Date.now() < deadline) {
        try {
            var current = currentPackage();
            if (current === pkg) {
                return;
            }
            if (current) {
                sawForeground = true; // 无障碍可用但前台是别的应用
            }
        } catch (e) { /* ignore */ }
        sleepMs(200);
    }
    if (!sawForeground) {
        if (isIOS) { fail(ERR.E2008, "启动应用后无法确认前台应用: " + pkg); }
        // currentPackage() 依赖无障碍服务；服务不可用时无法校验前台，
        // 不应误判为启动失败（后续步骤会因无障碍不可用而给出真实错误）。
        logLine(ctx, "start_app 无法校验前台（无障碍服务不可用），按启动成功继续: " + pkg);
        return;
    }
    fail(ERR.E2008, "启动应用后未进入前台: " + pkg);
};

ACTIONS.stop_app = function (ctx, step) {
    var isIOS = String(ctx.platform || "").toLowerCase() === "ios";
    var pkg = isIOS ? (step.args.bundle_id || step.args.app_id || step.args.package) : (step.args.package || step.args.app_id);
    if (!pkg) {
        fail(ERR.E2008, "stop_app 缺少可用的应用标识（app_key 未映射）: " + (step.args.app_key || ""));
    }
    // 策略 1：app.kill（需要 root 或 Shizuku）
    var killed = false;
    try {
        killed = app.kill(pkg);
    } catch (e) {
        killed = false;
    }
    if (killed) {
        return;
    }
    if (isIOS) { fail(ERR.E2008, "停止 iOS 应用失败: " + pkg); }
    // 策略 2：无障碍回退——打开应用详情页点击「强行停止」（无需 root）
    try {
        app.openAppSetting(pkg);
        sleepMs(1200);
        var stopBtn = text("强行停止").findOne(3000)
            || text("结束运行").findOne(2000)
            || text("Force stop").findOne(2000)
            || textContains("强行停止").findOne(2000);
        if (stopBtn) {
            stopBtn.click();
            sleepMs(900);
            var confirm = text("确定").findOne(2000)
                || text("强行停止").findOne(1000)
                || text("OK").findOne(1000);
            if (confirm) {
                confirm.click();
                sleepMs(500);
            }
            logLine(ctx, "stop_app 通过应用详情页「强行停止」完成: " + pkg);
            return;
        }
    } catch (e2) {
        // 回退失败则走统一失败路径
    }
    fail(ERR.E2008, "停止应用失败: " + pkg + "（app.kill 需要 root 或 Shizuku；应用详情页回退未找到「强行停止」按钮）");
};

// ---------------------------------------------------------------------------
// 步骤/用例执行
// ---------------------------------------------------------------------------
function executeStepOnce(ctx, step) {
    var action = String(step.action || "");
    if (["click", "input", "wait_until_exists", "assert_text", "assert_image",
        "click_image", "extract_by_ocr", "sleep", "swipe", "back", "home",
        "start_app", "stop_app"].indexOf(action) < 0) {
        fail(ERR.P1002, "不支持的动作: " + action);
    }
    var handler = ACTIONS[action];
    if (!handler) {
        fail(ERR.P1002, "不支持的动作: " + action);
    }
    return withImageScope(function () { return handler(ctx, step); });
}

var UNRESOLVED_VAR_RE = /\{\{\s*([A-Z0-9_]+)\s*\}\}/g;

function renderRuntimeValue(ctx, value) {
    // 运行时变量渲染：{{KEY}} 命中 ctx.exportedVariables 时替换，
    // 未命中保持原样（与平台 render_step_data 语义一致）。
    if (typeof value === "string") {
        return value.replace(UNRESOLVED_VAR_RE, function (whole, key) {
            var replacement = ctx.exportedVariables[key];
            return isNullish(replacement) ? whole : String(replacement);
        });
    }
    if (value && typeof value === "object") {
        if (value instanceof Array) {
            var list = [];
            for (var i = 0; i < value.length; i++) {
                list.push(renderRuntimeValue(ctx, value[i]));
            }
            return list;
        }
        var obj = {};
        for (var k in value) {
            obj[k] = renderRuntimeValue(ctx, value[k]);
        }
        return obj;
    }
    return value;
}

function renderStep(ctx, step) {
    var rendered = renderRuntimeValue(ctx, step);
    return rendered;
}

function stepPlatformAllowed(ctx, step) {
    // 目标平台来自运行配置（Android 引擎默认 android；iOS 端 WDA fork 传 ios）
    var target = String((ctx && ctx.platform) || "android").toLowerCase();
    var executeOn = step.execute_on || ["android", "ios"];
    for (var i = 0; i < executeOn.length; i++) {
        if (String(executeOn[i]).toLowerCase() === target) return true;
    }
    return false;
}

function stopRequested(ctx) {
    if (typeof global !== "undefined" && typeof global.__AUTODROID_SHOULD_STOP__ === "function"
        && global.__AUTODROID_SHOULD_STOP__()) {
        ctx.aborted = true;
    }
    return !!ctx.aborted;
}

function runStep(ctx, rawStep) {
    var step = renderStep(ctx, rawStep);
    var order = step.order;
    var action = String(step.action || "");
    var description = step.description || action;
    var result = {
        order: order,
        action: action,
        description: description,
        status: "FAIL",
        duration: 0,
        attempts: 1,
        error: null,
        error_code: null,
        error_strategy: String(step.error_strategy || "ABORT").toUpperCase(),
        screenshot_base64: null,
        output: null
    };

    if (!stepPlatformAllowed(ctx, step)) {
        result.status = "SKIP";
        result.error_code = "P1001_PLATFORM_NOT_ALLOWED";
        logLine(ctx, "步骤 " + order + " SKIP（平台不允许）: " + description);
        return result;
    }

    var retryCount = Math.max(0, Math.min(3, Number(step.retry_count || 0)));
    var totalAttempts = 1 + retryCount;
    var startedAt = Date.now();
    var lastError = null;
    var lastOutput = null;

    for (var attempt = 1; attempt <= totalAttempts; attempt++) {
        if (stopRequested(ctx)) {
            result.error = "用户请求停止执行";
            result.error_code = "USER_STOPPED";
            result.error_strategy = "ABORT";
            logLine(ctx, "执行已停止，未执行步骤 " + order);
            return result;
        }
        try {
            lastOutput = executeStepOnce(ctx, step);
            result.status = "PASS";
            result.attempts = attempt;
            lastError = null;
            break;
        } catch (e) {
            lastError = e;
            result.attempts = attempt;
            if (attempt < totalAttempts) {
                logLine(ctx, "步骤 " + order + " 第 " + attempt + " 次失败，1s 后重试: " + (e.message || e));
                sleepMs(RETRY_BACKOFF_MS);
            }
        }
    }
    result.duration = (Date.now() - startedAt) / 1000;

    if (lastError) {
        result.status = "FAIL";
        result.error = lastError.message || String(lastError);
        result.error_code = lastError.code || ERR.E2999;
        result.screenshot_base64 = screenshotBase64Jpeg(ctx);
        var strategy = String(step.error_strategy || "ABORT").toUpperCase();
        if (strategy === "IGNORE") {
            result.status = "WARNING";
            logLine(ctx, "步骤 " + order + " 失败（IGNORE 降级为 WARNING）: " + result.error);
        } else {
            logLine(ctx, "步骤 " + order + " FAIL: " + result.error);
        }
    } else {
        if (lastOutput && lastOutput.output) {
            result.output = lastOutput.output;
        }
        logLine(ctx, "步骤 " + order + " PASS: " + description + " (" + result.duration.toFixed(2) + "s)");
    }
    return result;
}

function runCase(ctx, caseScript, entry) {
    var caseResult = {
        case_id: caseScript.case_id,
        name: caseScript.name,
        alias: entry && entry.alias ? entry.alias : null,
        success: true,
        steps: [],
        exported_variables: null
    };
    logLine(ctx, "===== 开始执行用例: " + caseScript.name + " =====");

    var steps = caseScript.steps || [];
    for (var i = 0; i < steps.length; i++) {
        var step = steps[i];
        var stepResult = runStep(ctx, step);
        caseResult.steps.push(stepResult);
        if (ctx.aborted) {
            caseResult.success = false;
            break;
        }
        if (stepResult.status === "FAIL") {
            caseResult.success = false;
            var strategy = String(step.error_strategy || "ABORT").toUpperCase();
            if (strategy === "ABORT") {
                logLine(ctx, "步骤 " + step.order + " ABORT：终止用例执行");
                break;
            }
        }
    }
    var snapshot = {};
    for (var key in ctx.exportedVariables) {
        snapshot[key] = ctx.exportedVariables[key];
    }
    caseResult.exported_variables = snapshot;
    logLine(ctx, "===== 用例结束: " + caseScript.name + " success=" + caseResult.success + " =====");
    ctx.caseResults.push(caseResult);
    return caseResult;
}

function runScenario(ctx, entries, modules) {
    // 镜像平台场景语义（backend/scenario_execution.py::_run_scenario_cross_platform）：
    // 用例失败时取最后一个失败（非 WARNING）步骤的 error_strategy；
    // CONTINUE 继续后续用例，其余终止场景。批量包（kind=batch）始终继续。
    var forceContinue = !!(ctx.manifest && ctx.manifest.kind === "batch");
    for (var i = 0; i < entries.length; i++) {
        var result = runCase(ctx, modules[i], entries[i]);
        if (ctx.aborted) break;
        if (!result.success) {
            var strategy = "ABORT";
            for (var j = result.steps.length - 1; j >= 0; j--) {
                if (result.steps[j].status === "FAIL") {
                    strategy = String(result.steps[j].error_strategy || "ABORT").toUpperCase();
                    break;
                }
            }
            if (strategy === "CONTINUE" || forceContinue) {
                logLine(ctx, "用例失败，继续后续用例（strategy=" + strategy + (forceContinue ? ", batch" : "") + "）");
                continue;
            }
            logLine(ctx, "用例失败且策略为 " + strategy + "，终止场景执行");
            break;
        }
    }
}

// ---------------------------------------------------------------------------
// 上报
// ---------------------------------------------------------------------------
function deviceInfo(ctx) {
    var info = {
        model: "",
        brand: "",
        android_version: "",
        sdk: 0,
        resolution: "",
        serial: ""
    };
    try { info.model = String(device.model || ""); } catch (e) { }
    try { info.brand = String(device.brand || ""); } catch (e) { }
    try { info.android_version = String(device.release || ""); } catch (e) { }
    try { info.sdk = Number(device.sdkInt || 0); } catch (e) { }
    try { info.resolution = device.width + "x" + device.height; } catch (e) { }
    try {
        var serial = String(device.serial || "");
        if (serial && serial !== "unknown") info.serial = serial;
    } catch (e) { }
    try {
        if (device.getAndroidId) info.android_id = String(device.getAndroidId());
    } catch (e) { }
    return info;
}

function buildReportPayload(ctx) {
    var success = true;
    var aborted = !!ctx.aborted;
    for (var i = 0; i < ctx.caseResults.length; i++) {
        if (!ctx.caseResults[i].success) success = false;
    }
    return {
        task_id: ctx.taskId || null,
        run_id: ctx.runId || null,
        notify: ctx.notify !== false,
        format_version: FORMAT_VERSION,
        platform: ctx.platform || "android",
        kind: ctx.manifest.kind,
        case_id: ctx.manifest.kind === "case" ? ctx.manifest.cases[0].id : null,
        scenario_id: ctx.manifest.kind === "scenario" ? ctx.manifest.scenario_id : null,
        device: deviceInfo(ctx),
        started_at: ctx.startedAt,
        finished_at: Date.now(),
        success: success,
        aborted: aborted,
        cases: ctx.caseResults,
        logs: ctx.logLines
    };
}

function uploadReport(ctx, payload) {
    if (!ctx.server || !ctx.token) {
        logLine(ctx, "未配置平台地址/Token，跳过上报（结果已写入 report.json）");
        return false;
    }
    var url = ctx.server.replace(/\/+$/, "") + "/api/device-runs/report";
    try {
        var res = http.post(url, payload, {
            headers: {
                "Authorization": "Bearer " + ctx.token,
                "Content-Type": "application/json"
            },
            contentType: "application/json",
            timeout: 30000
        });
        if (res && res.statusCode >= 200 && res.statusCode < 300) {
            logLine(ctx, "执行结果已上报平台");
            try {
                ctx.uploadResponse = JSON.parse(res.body.string());
            } catch (e) {
                ctx.uploadResponse = null;
            }
            return true;
        }
        var detail = "";
        if (res && res.statusCode === 0) {
            try { detail = String(res.body.string() || "").substring(0, 500); } catch (ignored) { }
        }
        ctx.uploadError = "HTTP " + (res ? res.statusCode : "?") + (detail ? "：" + detail : "");
        logLine(ctx, "上报失败: " + ctx.uploadError);
        return false;
    } catch (e) {
        ctx.uploadError = String(e.message || e);
        logLine(ctx, "上报失败: " + ctx.uploadError);
        return false;
    }
}

function finish(ctx) {
    var payload = buildReportPayload(ctx);
    var reportPath = files.join(ctx.baseDir, "report.json");
    try {
        files.write(reportPath, JSON.stringify(payload));
    } catch (e) {
        logLine(ctx, "写入 report.json 失败: " + (e.message || e));
    }
    var uploadOk = false;
    try {
        uploadOk = uploadReport(ctx, payload);
    } catch (e) {
        logLine(ctx, "上报异常: " + (e.message || e));
    }
    if (uploadOk && ctx.uploadResponse) {
        // 回写上报响应（execution_id / report_id），供 App 端展示与跳转报告
        payload.upload_response = ctx.uploadResponse;
    }
    // Persist upload diagnostics on failure too; the app can retry this report.
    payload.upload_error = ctx.uploadError || null;
    try { files.write(reportPath, JSON.stringify(payload)); }
    catch (e) { logLine(ctx, "回写上报结果失败: " + (e.message || e)); }
    var summary = {
        aborted: payload.aborted,
        upload_ok: uploadOk,
        upload_error: ctx.uploadError || null,
        report_path: reportPath,
        success: payload.success,
        cases: payload.cases.length
    };
    console.log("[AutoDroid] 执行完成: success=" + payload.success + " upload=" + uploadOk);
    return summary;
}

// ---------------------------------------------------------------------------
// 上下文
// ---------------------------------------------------------------------------
function createContext(manifest, baseDir, embeddedAssets) {
    var runtimeConfig = {};
    try {
        if (typeof global !== "undefined" && global.__AUTODROID__) {
            runtimeConfig = global.__AUTODROID__;
        }
    } catch (e) { }
    if (!baseDir) {
        baseDir = runtimeConfig.baseDir || files.cwd();
    }
    var configPath = files.join(baseDir, "task_config.json");
    if (files.exists(configPath)) {
        try {
            var fileConfig = JSON.parse(files.read(configPath));
            for (var key in fileConfig) {
                if (isNullish(runtimeConfig[key])) runtimeConfig[key] = fileConfig[key];
            }
        } catch (e) { }
    }
    return {
        manifest: manifest,
        baseDir: baseDir,
        embeddedAssets: embeddedAssets || null,
        server: String(runtimeConfig.server || ""),
        token: String(runtimeConfig.token || ""),
        taskId: String(runtimeConfig.taskId || ""),
        // App 直跑模式：无 taskId，用客户端生成的 runId 做幂等标识
        runId: String(runtimeConfig.runId || ""),
        // 是否发送飞书通知（默认发送；App 可逐次关闭）
        notify: runtimeConfig.notify !== false,
        // 目标平台：android（默认，兼容既有 App）/ ios（WDA fork）
        platform: String(runtimeConfig.platform || (manifest && manifest.platform) || "android").toLowerCase(),
        caseResults: [],
        exportedVariables: {},
        logLines: [],
        hintCache: {},
        lastImageMatch: null,
        uploadResponse: null,
        startedAt: Date.now()
    };
}

var RUNTIME_API = {
    FORMAT_VERSION: FORMAT_VERSION,
    ERR: ERR,
    createContext: createContext,
    runCase: runCase,
    runScenario: runScenario,
    finish: finish
};

// 通过 require 加载时导出 CommonJS 模块；被直接运行（顶层脚本无 module）时仅挂全局，
// 保证 runtime.js 单独打开也不会抛 "module 未定义"。
if (typeof module !== "undefined" && module.exports) {
    module.exports = RUNTIME_API;
}
// 兼容部分 CommonJS 实现只读取初始 exports 对象的语义：把 API 复制进 exports。
if (typeof exports !== "undefined" && exports && typeof exports === "object" && exports !== RUNTIME_API) {
    for (var __apiKey in RUNTIME_API) {
        exports[__apiKey] = RUNTIME_API[__apiKey];
    }
}
