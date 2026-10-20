/*
 * AutoDroid iOS 适配层（随脚本包下发，由设备端执行器加载）
 *
 * 下发原因：适配层只依赖原生原语（__adt.*）与 runtime.js 的 API 约定，
 * 放在平台侧随包下发后，调整适配层不需要重新签名/安装 WebDriverAgent。
 * 设备端（AutoDroid-WDA）在 bundle 里读 adapter_js 并优先使用它。
 *
 * 原始出处：AutoDroid-WDA 仓库 autodroid/adapter.js（仅作历史参考）
 * ---------------------------------------------------------------------------
 * 目的：让平台下发的 runtime.js（Android 侧同一份执行器）在 iOS 上原样运行。
 * 做法：本文件在 JavaScriptCore 里先于 runtime.js 载入，实现 runtime.js 依赖的
 *      AutoJs6 API 表面（text/desc/id/className、automator、device、images、
 *      ocr、files、http、app、threads、console、sleep、java、android.util.Base64），
 *      全部转调原生原语 global.__adt.*（见 WebDriverAgentLib/AutoDroid/）。
 *
 * 约定：
 *   - runtime.js 不做任何修改，Android/iOS 共用，步骤语义天然一致；
 *   - 坐标单位统一为「截图像素」：device.width/height 返回截图像素尺寸，
 *     原生层在点击/滑动时按 scale 换算为点（point），JS 侧无感；
 *   - 图像句柄为 { __h: <nativeId>, width, height }，宽度/高度供 runtime.js 直接读取。
 */
(function (global) {
    "use strict";

    // JavaScriptCore has no Node/AutoJs-style `global` alias. Generated main.js
    // and runtime.js use it for configuration and the final execution result.
    global.global = global;

    var ADT = global.__adt;
    if (!ADT) {
        throw new Error("AutoDroid 适配层缺少原生原语 global.__adt");
    }

    // ------------------------------------------------------------------ //
    //  console / sleep
    // ------------------------------------------------------------------ //
    global.console = {
        log: function () { ADT.log("log", Array.prototype.join.call(arguments, " ")); },
        warn: function () { ADT.log("warn", Array.prototype.join.call(arguments, " ")); },
        error: function () { ADT.log("error", Array.prototype.join.call(arguments, " ")); },
        info: function () { ADT.log("info", Array.prototype.join.call(arguments, " ")); },
        debug: function () { ADT.log("debug", Array.prototype.join.call(arguments, " ")); }
    };
    global.sleep = function (ms) { ADT.sleep(Number(ms) || 0); };

    // ------------------------------------------------------------------ //
    //  Java / Android 兼容垫片（runtime.js 里少数几处平台调用）
    // ------------------------------------------------------------------ //
    function javaStringHashCode(value) {
        var text = String(value === undefined || value === null ? "" : value);
        var hash = 0;
        for (var i = 0; i < text.length; i++) {
            hash = (Math.imul(31, hash) + text.charCodeAt(i)) | 0;
        }
        return hash;
    }
    global.java = {
        lang: {
            String: function (value) {
                if (!(this instanceof global.java.lang.String)) {
                    return new global.java.lang.String(value);
                }
                this.value = String(value === undefined || value === null ? "" : value);
            }
        }
    };
    global.java.lang.String.prototype.hashCode = function () { return javaStringHashCode(this.value); };
    global.java.lang.String.prototype.toString = function () { return this.value; };
    global.java.lang.String.prototype.length = function () { return this.value.length; };

    global.android = {
        util: {
            Base64: {
                DEFAULT: 0,
                NO_WRAP: 2,
                decode: function (text) { return { __base64: String(text || "") }; },
                encodeToString: function (bytes) {
                    return bytes && bytes.__base64String ? bytes.__base64String : "";
                }
            }
        }
    };

    // ------------------------------------------------------------------ //
    //  选择器
    // ------------------------------------------------------------------ //
    function escapePredicateLiteral(value) {
        return String(value || "").replace(/\\/g, "\\\\").replace(/'/g, "\\'");
    }

    function textPredicate(exact, value) {
        var literal = escapePredicateLiteral(value);
        if (exact) {
            return "(label == '" + literal + "' OR name == '" + literal + "' OR value == '" + literal + "')";
        }
        return "(label CONTAINS[c] '" + literal + "' OR name CONTAINS[c] '" + literal
            + "' OR value CONTAINS[c] '" + literal + "')";
    }

    function regexPredicate(pattern) {
        var literal = escapePredicateLiteral(pattern);
        return "(label MATCHES '" + literal + "' OR name MATCHES '" + literal + "' OR value MATCHES '" + literal + "')";
    }

    function Element(spec) {
        this.__id = spec.id;                 // WDA 元素句柄（原生查询得到时存在）
        this.__meta = spec.meta || null;      // source dump 得到的只读节点 {text,desc,bounds}
    }

    Element.prototype.__attribute = function (name) {
        if (this.__meta) {
            if (name === "text") return this.__meta.text || "";
            if (name === "desc") return this.__meta.desc || "";
            return "";
        }
        return String(ADT.elementAttribute(this.__id, name) || "");
    };

    Element.prototype.text = function () { return this.__attribute("text"); };
    Element.prototype.desc = function () { return this.__attribute("desc"); };
    Element.prototype.id = function () {
        if (this.__meta) return "";
        return String(ADT.elementAttribute(this.__id, "identifier") || "");
    };
    Element.prototype.className = function () {
        if (this.__meta) return this.__meta.type || "";
        return String(ADT.elementAttribute(this.__id, "type") || "");
    };
    Element.prototype.bounds = function () {
        var rect = this.__meta ? this.__meta.bounds : ADT.elementRect(this.__id);
        if (!rect) {
            return { left: 0, top: 0, right: 0, bottom: 0, width: 0, height: 0, centerX: 0, centerY: 0 };
        }
        var left = Number(rect.x) || 0;
        var top = Number(rect.y) || 0;
        var width = Number(rect.width) || 0;
        var height = Number(rect.height) || 0;
        return {
            left: left, top: top, right: left + width, bottom: top + height,
            width: width, height: height,
            centerX: left + width / 2, centerY: top + height / 2
        };
    };
    Element.prototype.click = function () {
        if (this.__meta) return false;             // source dump 节点不可点击
        return !!ADT.elementClick(this.__id);
    };
    Element.prototype.longClick = function () {
        if (this.__meta) return false;
        return !!ADT.elementLongClick(this.__id);
    };
    Element.prototype.setText = function (text) {
        if (this.__meta) return false;
        return !!ADT.elementSetText(this.__id, String(text === undefined || text === null ? "" : text));
    };
    Element.prototype.exists = function () { return true; };

    function Selector(spec) {
        this.__spec = spec;      // {kind, value, all}
    }

    Selector.prototype.findOne = function (timeoutMs) {
        var timeout = Math.max(0, Number(timeoutMs) || 0);
        var deadline = Date.now() + timeout;
        for (;;) {
            var ids = ADT.findElements(JSON.stringify(this.__spec)) || [];
            if (ids.length) {
                return new Element({ id: ids[0] });
            }
            if (Date.now() >= deadline) {
                return null;
            }
            ADT.sleep(200);
        }
    };

    Selector.prototype.findOnce = function () { return this.findOne(0); };

    Selector.prototype.exists = function () { return !!this.findOne(0); };

    Selector.prototype.find = function () {
        if (this.__spec.all) {
            // textMatches(".*") 场景（页面签名）：一次 XML dump，避免逐节点查询
            var nodes = ADT.sourceNodes() || [];
            var out = [];
            for (var i = 0; i < nodes.length; i++) {
                out.push(new Element({ meta: nodes[i] }));
            }
            return out;
        }
        var ids = ADT.findElements(JSON.stringify(this.__spec)) || [];
        var list = [];
        for (var j = 0; j < ids.length; j++) {
            list.push(new Element({ id: ids[j] }));
        }
        return list;
    };

    global.text = function (value) { return new Selector({ kind: "predicate", value: textPredicate(true, value) }); };
    global.textContains = function (value) { return new Selector({ kind: "predicate", value: textPredicate(false, value) }); };
    // iOS 没有独立的 content-desc：无障碍 label/name 即描述，映射到同一套断言
    global.desc = function (value) { return new Selector({ kind: "predicate", value: textPredicate(true, value) }); };
    global.descContains = function (value) { return new Selector({ kind: "predicate", value: textPredicate(false, value) }); };
    global.id = function (value) { return new Selector({ kind: "accessibility id", value: String(value) }); };
    global.className = function (value) { return new Selector({ kind: "class name", value: String(value) }); };
    global.textMatches = function (pattern) {
        var source = String(pattern);
        return new Selector({ kind: "predicate", value: regexPredicate(source), all: source === ".*" });
    };

    // ------------------------------------------------------------------ //
    //  手势 / 系统导航（坐标为截图像素，原生层按 scale 换算）
    // ------------------------------------------------------------------ //
    global.click = function (x, y) { return !!ADT.tap(Number(x) || 0, Number(y) || 0); };
    global.longClick = function (x, y) { return !!ADT.longPress(Number(x) || 0, Number(y) || 0); };
    global.swipe = function (x1, y1, x2, y2, duration) {
        return !!ADT.drag(Number(x1) || 0, Number(y1) || 0, Number(x2) || 0, Number(y2) || 0, Number(duration) || 300);
    };
    global.back = function () { return !!ADT.pressBack(); };
    global.home = function () { return !!ADT.pressHome(); };
    global.currentPackage = function () { return String(ADT.activeApp() || ""); };
    global.currentActivity = function () { return ""; };
    global.toast = function (message) { ADT.log("toast", String(message)); };

    // ------------------------------------------------------------------ //
    //  automator
    // ------------------------------------------------------------------ //
    global.automator = {
        input: function (text) { return !!ADT.typeText(String(text === undefined || text === null ? "" : text)); },
        captureScreen: function () { return global.images.captureScreen(); },
        click: function (x, y) { return global.click(x, y); },
        swipe: function (x1, y1, x2, y2, duration) { return global.swipe(x1, y1, x2, y2, duration); },
        back: function () { return global.back(); },
        home: function () { return global.home(); },
        press: function (x, y, duration) { return ADT.longPress(Number(x) || 0, Number(y) || 0, Number(duration) || 500); }
    };

    // ------------------------------------------------------------------ //
    //  device（宽高为截图像素，与 Android 侧一致）
    // ------------------------------------------------------------------ //
    var deviceInfo = ADT.deviceInfo() || {};
    global.device = {
        width: Number(deviceInfo.width) || 0,
        height: Number(deviceInfo.height) || 0,
        model: String(deviceInfo.model || ""),
        brand: String(deviceInfo.brand || ""),
        release: String(deviceInfo.release || ""),
        sdkInt: Number(deviceInfo.sdkInt) || 0,
        serial: String(deviceInfo.serial || ""),
        getAndroidId: function () { return String(deviceInfo.androidId || deviceInfo.serial || ""); },
        getImei: function () { return null; },
        keepScreenOn: function () { return true; },
        cancelKeepingAwake: function () { return true; }
    };
    global.device.__scale = Number(deviceInfo.scale) || 1;

    // ------------------------------------------------------------------ //
    //  images / ocr
    // ------------------------------------------------------------------ //
    var imageScopes = {}, imageSequence = 0;
    function wrapImage(handle) {
        if (!handle) return null;
        imageScopes[handle.id] = ++imageSequence;
        return { __h: handle.id, width: Number(handle.width) || 0, height: Number(handle.height) || 0 };
    }

    global.images = {
        __beginScope: function () { return imageSequence; },
        __endScope: function (scope) {
            Object.keys(imageScopes).forEach(function (id) {
                if (imageScopes[id] > scope) {
                    ADT.releaseImage(Number(id)); delete imageScopes[id];
                }
            });
        },
        captureScreen: function () { return wrapImage(ADT.screenshot()); },
        requestScreenCapture: function () { return true; },
        read: function (path) { return wrapImage(ADT.imageFromFile(String(path))); },
        decode: function (base64) { return wrapImage(ADT.imageFromBase64(String(base64))); },
        grayscale: function (image) { return wrapImage(ADT.imageGrayscale(image.__h)); },
        clip: function (image, x, y, width, height) {
            return wrapImage(ADT.imageClip(image.__h, Number(x) || 0, Number(y) || 0, Number(width) || 0, Number(height) || 0));
        },
        scale: function (image, sx, sy) { return wrapImage(ADT.imageScale(image.__h, Number(sx) || 1, Number(sy) || 1)); },
        rotate: function (image) { return image; },
        toBase64: function (image, format, quality) {
            return String(ADT.imageToBase64(image.__h, String(format || "png"), Number(quality) || 100) || "");
        },
        save: function (image, path) { return !!ADT.imageSave(image.__h, String(path), true); },
        matchTemplate: function (image, template, options) {
            var opts = options || {};
            var match = ADT.matchTemplate(
                image.__h,
                template.__h,
                opts.threshold === undefined ? 0.9 : Number(opts.threshold),
                opts.max === undefined ? 1 : Number(opts.max)
            );
            var wrapped = null;
            if (match) {
                wrapped = {
                    similarity: Number(match.similarity) || 0,
                    point: { x: Number(match.x) || 0, y: Number(match.y) || 0 },
                    width: Number(match.width) || 0,
                    height: Number(match.height) || 0
                };
            }
            return {
                __all: wrapped ? [wrapped] : [],
                best: function () { return this.__all.length ? this.__all[0] : null; },
                matches: function () { return this.__all.slice(); }
            };
        },
        ssim: function (a, b) { return Number(ADT.ssim(a.__h, b.__h)) || 0; },
        findImage: function () { return null; },
        release: function (image) {
            if (image && image.__h !== undefined && imageScopes[image.__h] !== undefined) {
                ADT.releaseImage(image.__h); delete imageScopes[image.__h];
            }
        }
    };

    global.ocr = {
        recognizeText: function (image) {
            var texts = ADT.ocr(image.__h) || [];
            var out = [];
            for (var i = 0; i < texts.length; i++) { out.push(String(texts[i])); }
            return out;
        },
        recognizeTextFromBase64: function (base64) {
            var image = wrapImage(ADT.imageFromBase64(String(base64)));
            return global.ocr.recognizeText(image);
        }
    };

    // ------------------------------------------------------------------ //
    //  files（沙箱内：baseDir 为本次运行目录）
    // ------------------------------------------------------------------ //
    global.files = {
        cwd: function () { return String(ADT.baseDir() || ""); },
        getSdcardPath: function () { return String(ADT.baseDir() || ""); },
        join: function () {
            var parts = Array.prototype.slice.call(arguments);
            return ADT.fsJoin(parts);
        },
        exists: function (path) { return !!ADT.fsExists(String(path)); },
        read: function (path) { return String(ADT.fsRead(String(path)) || ""); },
        write: function (path, content) {
            return ADT.fsWrite(String(path), String(content === undefined || content === null ? "" : content));
        },
        append: function (path, content) {
            return ADT.fsAppend(String(path), String(content === undefined || content === null ? "" : content));
        },
        writeBytes: function (path, bytes) {
            var base64 = bytes && bytes.__base64 ? String(bytes.__base64) : null;
            return !!ADT.fsWriteBytes(String(path), base64);
        },
        readBytes: function () { throw new Error("iOS 适配层暂不支持 readBytes"); },
        ensureDir: function (path) { return !!ADT.fsEnsureDir(String(path)); },
        create: function (path) { return ADT.fsWrite(String(path), ""); },
        createWithDirs: function (path) { ADT.fsEnsureDir(String(path)); return ADT.fsWrite(String(path), ""); },
        remove: function (path) { return !!ADT.fsRemove(String(path)); },
        removeDir: function (path) { return !!ADT.fsRemove(String(path)); },
        isFile: function (path) { return !!ADT.fsIsFile(String(path)); },
        isDir: function (path) { return !!ADT.fsIsDir(String(path)); },
        listDir: function (path) { return ADT.fsListDir(String(path)) || []; },
        getName: function (path) {
            var text = String(path || "");
            var index = text.lastIndexOf("/");
            return index >= 0 ? text.substring(index + 1) : text;
        },
        getSdcardPathExists: function () { return true; }
    };

    // ------------------------------------------------------------------ //
    //  http（runtime.js 仅用 post 上报）
    // ------------------------------------------------------------------ //
    function wrapResponse(result) {
        var status = Number(result && result.status) || 0;
        var body = String((result && result.body) || "");
        return {
            statusCode: status,
            status: status,
            body: { string: function () { return body; }, json: function () { return JSON.parse(body); } },
            headers: {},
            message: ""
        };
    }

    global.http = {
        post: function (url, payload, options) {
            var opts = options || {};
            var body = typeof payload === "string" ? payload : JSON.stringify(payload);
            return wrapResponse(ADT.httpRequest(
                "POST", String(url), body,
                JSON.stringify(opts.headers || {}),
                Number(opts.timeout) || 30000
            ));
        },
        get: function (url, options) {
            var opts = options || {};
            return wrapResponse(ADT.httpRequest(
                "GET", String(url), "",
                JSON.stringify(opts.headers || {}),
                Number(opts.timeout) || 30000
            ));
        }
    };

    // ------------------------------------------------------------------ //
    //  app / threads
    // ------------------------------------------------------------------ //
    global.app = {
        launchPackage: function (bundleId) { return !!ADT.launchApp(String(bundleId)); },
        launch: function (bundleId) { return !!ADT.launchApp(String(bundleId)); },
        launchApp: function (bundleId) { return !!ADT.launchApp(String(bundleId)); },
        kill: function (bundleId) { return !!ADT.terminateApp(String(bundleId)); },
        openAppSetting: function (bundleId) {
            // iOS 没有「应用详情页」，stop_app 依赖的第二个回退策略在 iOS 上直接走失败路径
            ADT.log("warn", "iOS 不支持打开应用详情页，忽略: " + bundleId);
            return false;
        },
        getPackageName: function (bundleId) { return String(bundleId); }
    };

    global.threads = {
        start: function (worker) {
            var holder = {
                interrupt: function () { },
                isAlive: function () { return false; },
                join: function () { }
            };
            if (typeof worker === "function") {
                // iOS 侧截图/元素查询本身带超时保护，无需真线程；
                // 同步执行即可满足 runtime.js 的 holder.done 轮询语义。
                try {
                    worker();
                } catch (error) {
                    ADT.log("error", "threads.start 执行异常: " + (error && error.message ? error.message : error));
                }
            }
            return holder;
        },
        shutDownAll: function () { },
        currentThread: function () { return null; }
    };

    global.setTimeout = function (callback) { return global.threads.start(callback); };
    global.setInterval = function () { return { cancel: function () { } }; };
    global.clearInterval = function () { };
    global.clearTimeout = function () { };
})(this);
