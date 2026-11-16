# 高精度图像定位与点击

`strict-v1` 以拒绝不确定目标为默认行为，覆盖平台 Python、Desktop vendor、Android 手机端 OpenCV 和 iOS WDA。它替换 Android 图像点击的 `ImageX.wait`，并移除了图像动作隐式改为全屏确认文字点击和点击后同位补点的行为。

## 使用方式

录制时圈选完整、稳定的图标或文字，调整红色点击点，再选择“验证定位”。验证只检查当前显示截图，不发送设备点击；绿色框代表唯一合格目标，多个候选会阻止确认。保存并点击时重新读取设备截图、确认两帧稳定，再提交一次点击。

可以在步骤的图像设置中按平台修改模板内点击位置和归一化搜索区域。模板内容变化后，需要重新截取并确认配置。纯色、过小、重复或背景过多的模板会被拒绝或提示调整。

旧步骤没有额外参数时继续点击模板中心，但采用严格匹配。旧桌面客户端缺少能力声明时，新前端会要求升级。手机端原生能力缺失、OpenCV 无法加载或模板 SHA 不一致时，执行明确失败，不切换到旧算法。

## 数据与接口

标准步骤使用 `args.image_options`，旧步骤使用 `options.image_options`。按 `android`、`ios` 或显式共享的 `default` 保存配置：

```json
{
  "image_path": "static/images/element_example.png",
  "image_options": {
    "android": {
      "template_sha256": "原始模板文件的64位SHA256",
      "tap_anchor": [0.2, 0.5],
      "source_size": [1080, 2400],
      "search_region": [0, 0, 0.5, 1]
    }
  }
}
```

`tap_anchor` 相对于模板；`search_region` 相对于当前截图，缺省全屏。截图宽高缩放比不一致超过 3% 时拒绝推测变形。坐标在最终原图尺度计算；iOS 仅在发送点击时转换为 WDA 点坐标。配置与原始模板文件 SHA 绑定，避免同路径换图后继续使用旧锚点。

裁图接口增量返回 `template_sha256`、`source_size` 和 `quality`。`POST /api/device/match_image` 接收 `screenshot_base64`、`image_path` 和已经选定平台的扁平 `image_options`；返回状态、候选框、各项指标、拟点击点、质量与耗时。它不获取设备，也不调用执行器。

Desktop `/api/client/info` 返回 `image_matcher: {version, validated, preview, metadata}`。手机端使用 `images.strictMatcherCapabilities()`，并携带共享验收集指纹。缺少能力时图像动作返回不支持，不忽略新参数。

成功与失败的图像诊断都进入步骤结果 `output.image_match`。错误码：

| 错误码 | 含义 |
|---|---|
| E2005 | 没有通过验证的候选 |
| E2010 | 多个相似目标或证据不足以消歧 |
| E2011 | 模板无效或缺少可识别信息 |
| E2012 | 目标不稳定或超时 |
| E2013 | 当前配置或运行端缺少必要能力 |
| E2201 | 配置无效、模板 SHA 已变化 |

## 算法和执行约束

归一化去均值相关搜索多个局部候选，在原始像素分辨率验证双向轮廓覆盖率、前景误差、局部块误差和适用时的空间颜色误差。验证后合并同一对象的多尺度候选；多个独立合格目标或最佳候选优势不足时拒绝点击。

前景复核同时检查 4×4 分块和 16×16 像素重叠窗口（步长 8 像素，至少 8 个前景像素，误差上限 0.22），避免共享按钮边框、长标签掩盖单个数字差异。固定 PNG 回归包含 OpenCV 5 绘制的 `Order 1` / `Order 2`，不依赖测试机器的字体绘制版本；相同文字与轻微 JPEG 差异仍须通过。

参数集中为版本化 profile；不会在重试时降低阈值。两次截图间隔约 150 ms，局部框 IoU 至少 0.9，拟点击点偏移不超过 `max(2px, 模板短边×2%)`，且每帧均须通过完整结构验证。整个步骤的重试和退避共享超时预算。

取消后不得提交点击。Android 使用受 deadline 约束的单次 ADB 隧道 JSON-RPC；iOS 使用单次 WDA HTTP 请求，避免 SDK 自动恢复时重发有副作用的点击。提交结果未知时禁止自动重放步骤。

图像断言复用严格定位，但保持存在性语义：任意合格候选可证明“存在”；“不存在”需要连续两次明确未找到。多个合格目标可通过存在断言。模板无效、能力不足、定位异常或不确定结果均不能证明不存在。

## 验证与发布

生成与执行离线基准：

```sh
.venv/bin/python scripts/maintenance/benchmark_image_match.py generate --directory /tmp/image-calibration --split calibration
.venv/bin/python scripts/maintenance/benchmark_image_match.py generate --directory /tmp/image-acceptance --split acceptance
.venv/bin/python scripts/maintenance/benchmark_image_match.py evaluate --directory /tmp/image-acceptance
```

校准集和验收集使用不同模板家族，各包含 200 正例和 1,000 负例。冻结 profile 在校准集达到零误接受、100% 正例召回，不需要进一步放宽。2026-11-16 的共同验收额外加入 19 个真实模板空白负例、2 个真实搜索错配截图负例和 1 个真实签到正例，共 201 正例、1,021 负例。

平台、Desktop、Android 真实 OpenCV JNI 与 iOS 原生 matcher 在共同集均达到 201 正例正确定位、负例零误接受。生成样本覆盖明确的工程边界，不等于 200 个独立生产页面，也不承诺未来页面零误点。正式发布仍应先灰度观察真实业务模板的拒绝率和耗时；失败时暂停受影响能力，不回退宽松匹配。

本次验证结果和可重放数据位于工作区 `artifacts/image-click-audit-20261116/`。本次仅修改源码和本地验证，未发布桌面更新、安装物理设备 App 或修改线上服务。
