// Android 设备端点击兜底链试验台（无真机）。
// 模拟三星 One UI 场景：文本节点 clickable=false 时 ACTION_CLICK 返回 false，
// 验证 runtime.js 的 clickNodeRobust 依次退到父级容器 / 元素中心坐标。
//
// 用法: MODE=<direct|parent|coords|coords-method|fail> node android_click_harness.mjs <runtime.js 路径>
// 输出: {"status":"PASS|FAIL","error":...,"clicks":[{"x":111,"y":222}]}
import { readFileSync } from 'node:fs';

const mode = process.env.MODE || 'direct';
const runtimePath = process.argv[2];
const clicks = [];

// ---- AutoJs6 风格的全局桩 ----
global.images = { __beginScope: () => null, __endScope: () => {} };
global.files = {
  join: (...a) => a.join('/'), exists: () => false, read: () => '',
  cwd: () => '.', isFile: () => false, list: () => [],
};
global.click = (x, y) => { clicks.push({ x, y, via: 'global.click' }); return true; };
global.press = (x, y, d) => { clicks.push({ x, y, d, via: 'press' }); return true; };

const parentEl = {
  click: () => mode === 'parent',
  parent: () => null,
  bounds: () => ({ centerX: 0, centerY: 0 }),
};
const el = {
  click: () => false, // 三星场景：文本节点不可点击
  parent: () => (mode === 'fail' ? null : parentEl),
  bounds: () => {
    if (mode === 'coords') return { centerX: 111, centerY: 222 }; // 属性风格（iOS 适配层）
    if (mode === 'coords-method') return { centerX: () => 333, centerY: () => 444 }; // 方法风格（Android Rect）
    return { centerX: 0, centerY: 0 };
  },
};
if (mode === 'direct') el.click = () => true;
if (mode === 'coords' || mode === 'coords-method') parentEl.click = () => false;

global.text = () => ({ findOne: () => el, findOnce: () => el });
global.textContains = () => ({ findOne: () => el, findOnce: () => el });
global.desc = () => ({ findOne: () => null, findOnce: () => null });
global.descContains = () => ({ findOne: () => null, findOnce: () => null });

// ---- 载入 runtime.js 并跑一个 click 步骤 ----
const src = readFileSync(runtimePath, 'utf8');
const mod = { exports: {} };
new Function('module', 'exports', src)(mod, mod.exports);
const RT = mod.exports;

const manifest = {
  format_version: 1, kind: 'case', platform: 'android', variables: {},
  cases: [{ id: 1, name: 'click-fallback', file: 'x' }],
};
const ctx = RT.createContext(manifest, '.');
const result = RT.runCase(ctx, {
  case_id: 1, name: 'click-fallback', variables: {},
  steps: [{
    order: 1, action: 'click', timeout: 1, error_strategy: 'ABORT', retry_count: 0,
    execute_on: ['android', 'ios'],
    locators: [{ by: 'text', selector: '首页' }], args: {},
  }],
}, { id: 1, name: 'click-fallback' });

const step = result.steps[0];
console.log(JSON.stringify({ status: step.status, error: step.error, clicks }));
