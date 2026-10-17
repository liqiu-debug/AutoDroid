/*
 * iOS 设备端执行器「无真机」集成试验台
 * ---------------------------------------------------------------------------
 * 用 Node 扮演 WebDriverAgent 的原生原语（global.__adt.*），把平台下发的脚本包
 * 按设备端同样的顺序执行：adapter_js → main.js（内部再加载 runtime.js）。
 *
 * 验证的是除原生层以外的全部逻辑：脚本包结构、runtime.js 步骤语义、适配层 API
 * 覆盖面、报告回传载荷。原生层（XCTest 实际操作）仍需真机/模拟器验证。
 *
 * 用法：node ios_runtime_harness.mjs <bundle.json> [--workdir <dir>]
 * 输出：一行 JSON 到 stdout：{ok, exception, logs, reportPayload, result, apiCalls}
 */
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import vm from 'node:vm';

const bundlePath = process.argv[2];
const workdirArgIndex = process.argv.indexOf('--workdir');
const workdir = workdirArgIndex >= 0 ? process.argv[workdirArgIndex + 1] : fs.mkdtempSync(path.join(os.tmpdir(), 'adt-harness-'));

if (!bundlePath) {
  console.error('用法: node ios_runtime_harness.mjs <bundle.json> [--workdir <dir>]');
  process.exit(2);
}

const bundle = JSON.parse(fs.readFileSync(bundlePath, 'utf8'));

// --------------------------------------------------------------------------
// 设备端文件系统：按脚本包内容落盘（与 ADTRunner.prepareRunDirectory 一致）
// --------------------------------------------------------------------------
const baseDir = path.join(workdir, `run-${Date.now()}`);
fs.mkdirSync(baseDir, { recursive: true });
fs.writeFileSync(path.join(baseDir, 'manifest.json'), JSON.stringify(bundle.manifest, null, 2));
fs.writeFileSync(path.join(baseDir, 'runtime.js'), bundle.runtime_js || '');
fs.writeFileSync(path.join(baseDir, 'main.js'), bundle.main_js || '');
if (bundle.adapter_js) {
  fs.writeFileSync(path.join(baseDir, 'adapter.js'), bundle.adapter_js);
}
for (const entry of bundle.case_files || []) {
  const target = path.join(baseDir, entry.path);
  fs.mkdirSync(path.dirname(target), { recursive: true });
  fs.writeFileSync(target, entry.content);
}
for (const [name, base64] of Object.entries(bundle.assets || {})) {
  const target = path.join(baseDir, 'assets', name);
  fs.mkdirSync(path.dirname(target), { recursive: true });
  fs.writeFileSync(target, Buffer.from(base64, 'base64'));
}

// --------------------------------------------------------------------------
// 原生原语桩：记录调用，返回可用的假数据
// --------------------------------------------------------------------------
const logs = [];
const apiCalls = [];
let elementSeq = 1;
let imageSeq = 1;
const elementRegistry = new Map();
let reportPayload = null;

function record(name, args, result) {
  apiCalls.push({ name, args: args === undefined ? null : args, result: result === undefined ? null : result });
}

const sleepSync = (ms) => {
  if (ms > 0) {
    // 试验台里不真等，避免自检包里的 sleep 拖慢测试
  }
};

const resolvePath = (value) => {
  if (!value) return baseDir;
  return path.isAbsolute(value) ? value : path.join(baseDir, value);
};

const adt = {
  log(level, message) {
    logs.push(`[${level}] ${message}`);
    record('log', { level, message });
  },
  sleep(ms) {
    record('sleep', { ms });
    sleepSync(ms);
  },
  deviceInfo() {
    const info = {
      width: 1170,
      height: 2532,
      scale: 3,
      model: 'iPhone17,1',
      brand: 'Apple',
      release: '26.1',
      sdkInt: 26,
      serial: 'ios:HARNESS',
      androidId: 'ios:HARNESS'
    };
    record('deviceInfo', null, info);
    return info;
  },
  screenshot() {
    const handle = { id: imageSeq, width: 1170, height: 2532 };
    imageSeq += 1;
    record('screenshot', null, handle);
    return handle;
  },
  imageFromFile(filePath) {
    const handle = { id: imageSeq, width: 40, height: 40 };
    imageSeq += 1;
    record('imageFromFile', { filePath }, handle);
    return fs.existsSync(resolvePath(filePath)) ? handle : null;
  },
  imageFromBase64() {
    const handle = { id: imageSeq, width: 40, height: 40 };
    imageSeq += 1;
    return handle;
  },
  imageGrayscale(handle) {
    record('imageGrayscale', { handle });
    return { id: imageSeq++, width: 1170, height: 2532 };
  },
  imageClip(handle, x, y, width, height) {
    record('imageClip', { handle, x, y, width, height });
    return { id: imageSeq++, width: Math.max(1, width), height: Math.max(1, height) };
  },
  imageScale(handle, sx, sy) {
    record('imageScale', { handle, sx, sy });
    return { id: imageSeq++, width: 200, height: 100 };
  },
  imageToBase64() {
    record('imageToBase64');
    return 'aGFybmVzcy1pbWFnZQ==';
  },
  imageSave() {
    return true;
  },
  releaseImage() {},
  matchTemplate() {
    record('matchTemplate');
    return null;
  },
  ssim() {
    record('ssim');
    return 0.98;
  },
  ocr() {
    record('ocr');
    return ['设置', '通用', '关于本机', 'HARNESS'];
  },
  findElements(specJson) {
    const spec = JSON.parse(specJson || '{}');
    record('findElements', spec);
    // 前两次查询返回一个元素，模拟「元素存在」；随后返回空，避免死循环
    if (elementRegistry.size < 2) {
      const id = elementSeq++;
      elementRegistry.set(id, spec);
      return [id];
    }
    return [];
  },
  elementAttribute(elementId, name) {
    record('elementAttribute', { elementId, name });
    if (name === 'text') return '设置';
    if (name === 'desc' || name === 'name') return '设置';
    if (name === 'identifier') return 'selftest-target';
    if (name === 'type') return 'XCUIElementTypeButton';
    return '';
  },
  elementRect() {
    record('elementRect');
    return { x: 10, y: 20, width: 100, height: 44 };
  },
  elementClick() {
    record('elementClick');
    return true;
  },
  elementLongClick() {
    record('elementLongClick');
    return true;
  },
  elementSetText(elementId, text) {
    record('elementSetText', { elementId, text });
    return true;
  },
  sourceNodes() {
    record('sourceNodes');
    return [
      { type: 'XCUIElementTypeStaticText', text: '设置', desc: '设置', bounds: { x: 10, y: 20, width: 100, height: 44 } },
      { type: 'XCUIElementTypeButton', text: '通用', desc: '通用', bounds: { x: 10, y: 80, width: 120, height: 44 } }
    ];
  },
  tap(x, y) {
    record('tap', { x, y });
    return true;
  },
  longPress(x, y, duration) {
    record('longPress', { x, y, duration });
    return true;
  },
  drag(x1, y1, x2, y2, duration) {
    record('drag', { x1, y1, x2, y2, duration });
    return true;
  },
  pressBack() {
    record('pressBack');
    return true;
  },
  pressHome() {
    record('pressHome');
    return true;
  },
  activeApp() {
    record('activeApp');
    return 'com.apple.Preferences';
  },
  launchApp(bundleId) {
    record('launchApp', { bundleId });
    return true;
  },
  terminateApp(bundleId) {
    record('terminateApp', { bundleId });
    return true;
  },
  typeText(text) {
    record('typeText', { text });
    return true;
  },
  httpRequest(method, url, body, headers, timeout) {
    record('httpRequest', { method, url, bodyLength: (body || '').length, headers, timeout });
    if ((url || '').includes('/api/device-runs/report')) {
      try {
        reportPayload = JSON.parse(body || '{}');
      } catch (error) {
        reportPayload = { parseError: String(error) };
      }
      return { status: 200, body: JSON.stringify({ execution_id: 999, report_id: 1, message: 'harness' }) };
    }
    return { status: 200, body: '{}' };
  },
  fsExists(target) {
    return fs.existsSync(resolvePath(target));
  },
  fsRead(target) {
    const full = resolvePath(target);
    return fs.existsSync(full) ? fs.readFileSync(full, 'utf8') : '';
  },
  fsWrite(target, content) {
    const full = resolvePath(target);
    fs.mkdirSync(path.dirname(full), { recursive: true });
    fs.writeFileSync(full, content);
    return true;
  },
  fsAppend(target, content) {
    const full = resolvePath(target);
    fs.mkdirSync(path.dirname(full), { recursive: true });
    fs.appendFileSync(full, content);
    return true;
  },
  fsWriteBytes(target, base64) {
    const full = resolvePath(target);
    fs.mkdirSync(path.dirname(full), { recursive: true });
    fs.writeFileSync(full, Buffer.from(base64 || '', 'base64'));
    return true;
  },
  fsEnsureDir(target) {
    fs.mkdirSync(resolvePath(target), { recursive: true });
    return true;
  },
  fsRemove(target) {
    try {
      fs.rmSync(resolvePath(target), { recursive: true, force: true });
      return true;
    } catch (error) {
      return false;
    }
  },
  fsIsFile(target) {
    const full = resolvePath(target);
    return fs.existsSync(full) && fs.statSync(full).isFile();
  },
  fsIsDir(target) {
    const full = resolvePath(target);
    return fs.existsSync(full) && fs.statSync(full).isDirectory();
  },
  fsListDir(target) {
    const full = resolvePath(target);
    return fs.existsSync(full) ? fs.readdirSync(full) : [];
  },
  fsJoin(parts) {
    const list = (parts || []).map((item) => String(item)).filter((item) => item.length > 0);
    return list.length > 0 ? path.join(...list) : baseDir;
  },
  baseDir() {
    return baseDir;
  }
};

// --------------------------------------------------------------------------
// 执行：adapter_js → main.js
// --------------------------------------------------------------------------
const sandbox = {
  __adt: adt,
  __AUTODROID__: {
    baseDir,
    server: 'http://harness.local',
    token: 'harness-token',
    runId: 'harness-run',
    taskId: '',
    notify: false,
    platform: bundle.manifest?.platform || 'ios'
  }
};
sandbox.globalThis = sandbox;
sandbox.global = sandbox;
sandbox.setTimeout = (fn) => { try { fn(); } catch (error) { logs.push(`[harness-error] ${error}`); } return 0; };
const context = vm.createContext(sandbox);

let exception = null;
try {
  if (!bundle.adapter_js) {
    throw new Error('脚本包缺少 adapter_js（iOS 适配层必须随包下发）');
  }
  vm.runInContext(bundle.adapter_js, context, { filename: 'adapter.js' });
  vm.runInContext(bundle.main_js, context, { filename: 'main.js' });
} catch (error) {
  exception = `${error?.name || 'Error'}: ${error?.message || error}`;
}

const result = sandbox.__AUTODROID_RESULT__ ?? null;
const reportFile = path.join(baseDir, 'report.json');
const reportJson = fs.existsSync(reportFile) ? JSON.parse(fs.readFileSync(reportFile, 'utf8')) : null;

console.log(JSON.stringify({
  ok: exception === null && result !== null,
  exception,
  logs,
  apiCallNames: [...new Set(apiCalls.map((call) => call.name))],
  apiCallCount: apiCalls.length,
  result,
  reportPayload,
  reportJson,
  baseDir
}));
