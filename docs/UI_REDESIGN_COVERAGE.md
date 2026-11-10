# AutoDroid 全界面改版覆盖与验收记录

更新：2026-10-03。源代码清单按当前工作区生成；叶子路由 35 个，Vue 文件 77 个，其中活跃 74 个、无入口遗留 3 个。

本表将「实现」「编译」「行为测试」「浏览器视觉」分开。**已实现或编译通过不代表视觉验收通过。** 浏览器记录使用本地隔离示例数据，不连接真实设备、通知、接口或远端 AI；未检查的页面仍标为待验。

> 说明（2026-11）：本表是 2026-10-03 的改版覆盖台账，以下内容保留原文作为历史记录。其中涉及「模型化智能巡检」（`/special/inspection`、`/execution/reports/inspection/:id`、`InspectionLivePanel.vue`、巡检配置弹窗、巡检报告抽屉、报告中心智能巡检标签）、「接入远程设备」（Device Center 的 agentGuide 弹窗）与兼容性「链路回放」的条目，对应能力已于 2026-11 下线、相关页面与组件已移除；表中的路由/组件总数、行号锚点和验收状态均为当时的快照，不代表当前界面清单。

## 设计与验收基线

- 桌面：184px侧栏；13px表单、12px表格；常规36px行、双行48px；32px控件；6px/8px圆角；16px页距；灰白底、低饱和蓝。
- 密度：1440×900至少18个完整标准用例行、1366×768至少14行，100%缩放、数据充足、无额外提示条；隔离样本已实测为19行和15行，行高36px且无横向滚动。
- 移动：390px、320px及现有客户端切换；14px正文、16px输入、44px主要触控区域；原mobileAvailable能力边界保留。
- 角色与状态：管理员/普通用户、feature flag开/关、Android/iOS、加载/空数据/失败/运行/排队/终止、长名称、滚动、弹层操作可达。

## 35个叶子路由

| 路由 | 页面来源 | 边界/主要验收 | 实现 | SFC编译 | 浏览器视觉 |
|---|---|---|---|---|---|
| `/login` | [views/LoginView.vue](../frontend/src/views/LoginView.vue#L1) | 移动可用 | 已改 | 通过 | 待验 |
| `/register` | [views/login/Register.vue](../frontend/src/views/login/Register.vue#L1) | 移动可用 | 已改 | 通过 | 待验 |
| `/dashboard` | [views/dashboard/DashboardView.vue](../frontend/src/views/dashboard/DashboardView.vue#L1) | 移动可用 | 已改 | 通过 | 隔离样本：1440×900 |
| `/assets/devices` | [views/devices/DeviceCenter.vue](../frontend/src/views/devices/DeviceCenter.vue#L1) | 移动可用 | 已改 | 通过 | 待验 |
| `/assets/variables` | [views/variables/VariableLibrary.vue](../frontend/src/views/variables/VariableLibrary.vue#L1) | 移动沿用不可用提示 | 已改 | 通过 | 待验 |
| `/assets/packages` | [views/packages/PackageManagement.vue](../frontend/src/views/packages/PackageManagement.vue#L1) | 移动沿用不可用提示 | 已改 | 通过 | 待验 |
| `/ui/cases` | [views/cases/CaseList.vue](../frontend/src/views/cases/CaseList.vue#L1) | 移动可用 | 已改 | 通过 | 隔离样本：1440×900、1366×768 |
| `/ui/cases/create` | [views/cases/CaseEditor.vue](../frontend/src/views/cases/CaseEditor.vue#L1) | 移动沿用不可用提示；首次保存/返回 | 已改 | 通过 | 待验 |
| `/ui/cases/:id/edit` | [views/cases/CaseEditor.vue](../frontend/src/views/cases/CaseEditor.vue#L1) | 移动沿用不可用提示；直接打开/刷新/上级菜单高亮 | 已改 | 通过 | 隔离样本：1366×768 |
| `/ui/scenarios` | [views/scenarios/ScenarioList.vue](../frontend/src/views/scenarios/ScenarioList.vue#L1) | 移动可用 | 已改 | 通过 | 待验 |
| `/ui/scenarios/create` | [views/scenarios/ScenarioEditor.vue](../frontend/src/views/scenarios/ScenarioEditor.vue#L1) | 移动沿用不可用提示；首次保存/返回 | 已改 | 通过 | 待验 |
| `/ui/scenarios/:id/edit` | [views/scenarios/ScenarioEditor.vue](../frontend/src/views/scenarios/ScenarioEditor.vue#L1) | 移动沿用不可用提示；直接打开/刷新/上级菜单高亮 | 已改 | 通过 | 待验 |
| `/api-testing/interfaces` | [views/api-testing/AssetList.vue](../frontend/src/views/api-testing/AssetList.vue#L1) | 移动沿用不可用提示 | 已改 | 通过 | 待验 |
| `/api-testing/interfaces/create` | [views/api-testing/InterfaceEditor.vue](../frontend/src/views/api-testing/InterfaceEditor.vue#L1) | 移动沿用不可用提示；首次保存/返回 | 已改 | 通过 | 待验 |
| `/api-testing/interfaces/:id/edit` | [views/api-testing/InterfaceEditor.vue](../frontend/src/views/api-testing/InterfaceEditor.vue#L1) | 移动沿用不可用提示；直接打开/刷新/上级菜单高亮 | 已改 | 通过 | 待验 |
| `/api-testing/scenarios` | [views/api-testing/AssetList.vue](../frontend/src/views/api-testing/AssetList.vue#L1) | 移动沿用不可用提示 | 已改 | 通过 | 待验 |
| `/api-testing/scenarios/create` | [views/api-testing/ScenarioEditor.vue](../frontend/src/views/api-testing/ScenarioEditor.vue#L1) | 移动沿用不可用提示；首次保存/返回 | 已改 | 通过 | 待验 |
| `/api-testing/scenarios/:id/edit` | [views/api-testing/ScenarioEditor.vue](../frontend/src/views/api-testing/ScenarioEditor.vue#L1) | 移动沿用不可用提示；直接打开/刷新/上级菜单高亮 | 已改 | 通过 | 待验 |
| `/special/fastbot` | [views/special/Fastbot.vue](../frontend/src/views/special/Fastbot.vue#L1) | 移动沿用不可用提示 | 委托已改FastbotDashboard | 通过 | 待验 |
| `/special/inspection` | [views/special/Inspection.vue](../frontend/src/views/special/Inspection.vue#L1) | 移动沿用不可用提示；开关 model_inspection | 已改 | 通过 | 待验 |
| `/special/fastbot/report/:id` | [views/fastbot/FastbotReportDetail.vue](../frontend/src/views/fastbot/FastbotReportDetail.vue#L1) | 移动沿用不可用提示；直接打开/刷新/上级菜单高亮 | 已改 | 通过 | 待验 |
| `/special/fluency` | [views/special/Fluency.vue](../frontend/src/views/special/Fluency.vue#L1) | 移动沿用不可用提示 | 已改 | 通过 | 待验 |
| `/special/startup` | [views/special/Startup.vue](../frontend/src/views/special/Startup.vue#L1) | 移动沿用不可用提示 | 已改 | 通过 | 待验 |
| `/special/compatibility/run` | [views/special/Compatibility.vue](../frontend/src/views/special/Compatibility.vue#L1) | 移动沿用不可用提示 | 已改 | 通过 | 待验 |
| `/special/compatibility/page-sets` | [views/special/CompatibilityPageSets.vue](../frontend/src/views/special/CompatibilityPageSets.vue#L1) | 移动沿用不可用提示 | 已改 | 通过 | 待验 |
| `/execution/tasks` | [views/tasks/TaskList.vue](../frontend/src/views/tasks/TaskList.vue#L1) | 移动沿用不可用提示 | 已改 | 通过 | 待验 |
| `/execution/reports` | [views/reports/ReportList.vue](../frontend/src/views/reports/ReportList.vue#L1) | 移动可用 | 已改 | 通过 | 待验 |
| `/execution/reports/compatibility/:id` | [views/reports/CompatibilityReportDetail.vue](../frontend/src/views/reports/CompatibilityReportDetail.vue#L1) | 移动沿用不可用提示；直接打开/刷新/上级菜单高亮 | 已改 | 通过 | 待验 |
| `/execution/reports/api/:id` | [views/api-testing/RunDetail.vue](../frontend/src/views/api-testing/RunDetail.vue#L1) | 移动沿用不可用提示；直接打开/刷新/上级菜单高亮 | 已改 | 通过 | 待验 |
| `/execution/reports/inspection/:id` | [views/reports/InspectionReportDetail.vue](../frontend/src/views/reports/InspectionReportDetail.vue#L1) | 移动可用；开关 model_inspection；直接打开/刷新/上级菜单高亮 | 已改 | 通过 | 待验 |
| `/execution/reports/:id` | [views/reports/ReportDetail.vue](../frontend/src/views/reports/ReportDetail.vue#L1) | 移动可用；直接打开/刷新/上级菜单高亮 | 已改 | 通过 | 待验 |
| `/settings/users` | [views/admin/UserManagement.vue](../frontend/src/views/admin/UserManagement.vue#L1) | 移动沿用不可用提示；仅管理员 | 已改 | 通过 | 待验 |
| `/settings/notifications` | [views/settings/NotificationSettings.vue](../frontend/src/views/settings/NotificationSettings.vue#L1) | 移动沿用不可用提示 | 已改 | 通过 | 待验 |
| `/settings/tokens` | [views/settings/ApiTokens.vue](../frontend/src/views/settings/ApiTokens.vue#L1) | 移动沿用不可用提示 | 已改 | 通过 | 待验 |
| `/account/password` | [views/account/ChangePassword.vue](../frontend/src/views/account/ChangePassword.vue#L1) | 移动沿用不可用提示 | 已改 | 通过 | 待验 |

## 全部活跃嵌入页面、组件与布局

下列为除路由页之外的活跃 Vue。检查静态/动态 import 引用图，递归组件按父组件入口归属；复用组件在多个分支均需浏览器验证。

| 组件 | 活跃父入口 | 实现 | SFC编译 | 浏览器视觉 |
|---|---|---|---|---|
| [App.vue](../frontend/src/App.vue#L1) | 应用挂载 | 已改 | 通过 | 待验 |
| [components/ClientModeSwitch.vue](../frontend/src/components/ClientModeSwitch.vue#L1) | Index.vue、LoginView.vue、Register.vue | 已改 | 通过 | 待验 |
| [components/DeviceStage.vue](../frontend/src/components/DeviceStage.vue#L1) | CaseEditor.vue | 已改 | 通过 | 待验 |
| [components/FastbotReplayPlayer.vue](../frontend/src/components/FastbotReplayPlayer.vue#L1) | ReplayDialog.vue | 已改 | 通过 | 待验 |
| [components/FolderTreePanel.vue](../frontend/src/components/FolderTreePanel.vue#L1) | CaseList.vue、ScenarioList.vue | 已改 | 通过 | 待验 |
| [components/GeneralStepsPanel.vue](../frontend/src/components/GeneralStepsPanel.vue#L1) | CaseEditor.vue | 已改 | 通过 | 待验 |
| [components/InspectionLivePanel.vue](../frontend/src/components/InspectionLivePanel.vue#L1) | InspectionReportDetail.vue | 已改 | 通过 | 待验 |
| [components/IosMjpegPlayer.vue](../frontend/src/components/IosMjpegPlayer.vue#L1) | DeviceStage.vue | 已改 | 通过 | 待验 |
| [components/LogConsole.vue](../frontend/src/components/LogConsole.vue#L1) | CaseEditor.vue、ScenarioEditor.vue | 已改 | 通过 | 待验 |
| [components/MobileUnavailable.vue](../frontend/src/components/MobileUnavailable.vue#L1) | Index.vue | 已改 | 通过 | 待验 |
| [components/ScrcpyPlayer.vue](../frontend/src/components/ScrcpyPlayer.vue#L1) | DeviceStage.vue、InspectionLivePanel.vue、DeviceCenter.vue | 已改 | 通过 | 待验 |
| [components/StepBuilder.vue](../frontend/src/components/StepBuilder.vue#L1) | CaseEditor.vue | 已改 | 通过 | 待验 |
| [components/api-testing/AddToScenarioDialog.vue](../frontend/src/components/api-testing/AddToScenarioDialog.vue#L1) | AssetList.vue、InterfaceEditor.vue | 已改 | 通过 | 待验 |
| [components/api-testing/AiAssertions.vue](../frontend/src/components/api-testing/AiAssertions.vue#L1) | InterfaceEditor.vue、ScenarioEditor.vue | 已改 | 通过 | 待验 |
| [components/api-testing/AiFailureExplanation.vue](../frontend/src/components/api-testing/AiFailureExplanation.vue#L1) | RunDetail.vue | 已改 | 通过 | 待验 |
| [components/api-testing/AssertionsEditor.vue](../frontend/src/components/api-testing/AssertionsEditor.vue#L1) | RequestEditor.vue | 已改 | 通过 | 待验 |
| [components/api-testing/AssetMetadata.vue](../frontend/src/components/api-testing/AssetMetadata.vue#L1) | AssetList.vue、InterfaceEditor.vue、ScenarioEditor.vue | 已改 | 通过 | 待验 |
| [components/api-testing/CurlImportDialog.vue](../frontend/src/components/api-testing/CurlImportDialog.vue#L1) | InterfaceEditor.vue、ScenarioEditor.vue | 已改 | 通过 | 待验 |
| [components/api-testing/EnvironmentDrawer.vue](../frontend/src/components/api-testing/EnvironmentDrawer.vue#L1) | InterfaceEditor.vue、ScenarioEditor.vue | 已改 | 通过 | 待验 |
| [components/api-testing/JsonBodyEditor.vue](../frontend/src/components/api-testing/JsonBodyEditor.vue#L1) | RequestEditor.vue | 已改 | 通过 | 待验 |
| [components/api-testing/ParametersEditor.vue](../frontend/src/components/api-testing/ParametersEditor.vue#L1) | RequestEditor.vue | 已改 | 通过 | 待验 |
| [components/api-testing/ReferencePicker.vue](../frontend/src/components/api-testing/ReferencePicker.vue#L1) | AssertionsEditor.vue、ValueEditor.vue | 已改 | 通过 | 待验 |
| [components/api-testing/RequestEditor.vue](../frontend/src/components/api-testing/RequestEditor.vue#L1) | SaveInterfaceDialog.vue、InterfaceEditor.vue、ScenarioEditor.vue | 已改 | 通过 | 待验 |
| [components/api-testing/ResultPanel.vue](../frontend/src/components/api-testing/ResultPanel.vue#L1) | InterfaceEditor.vue、RunDetail.vue、ScenarioEditor.vue | 已改 | 通过 | 待验 |
| [components/api-testing/SaveInterfaceDialog.vue](../frontend/src/components/api-testing/SaveInterfaceDialog.vue#L1) | ScenarioEditor.vue | 已改 | 通过 | 待验 |
| [components/api-testing/ScheduleDrawer.vue](../frontend/src/components/api-testing/ScheduleDrawer.vue#L1) | ScenarioEditor.vue | 已改 | 通过 | 待验 |
| [components/api-testing/ValueEditor.vue](../frontend/src/components/api-testing/ValueEditor.vue#L1) | AssertionsEditor.vue、JsonBodyEditor.vue、ParametersEditor.vue、RequestEditor.vue | 已改 | 通过 | 待验 |
| [layout/Index.vue](../frontend/src/layout/Index.vue#L1) | router布局 | 已改 | 通过 | 待验 |
| [layout/components/Navbar.vue](../frontend/src/layout/components/Navbar.vue#L1) | Index.vue | 已改 | 通过 | 待验 |
| [layout/components/SidebarMenuItem.vue](../frontend/src/layout/components/SidebarMenuItem.vue#L1) | Index.vue | 保留递归/权限逻辑；无局部style，layout深度样式统一 | 通过 | 待验 |
| [views/api-testing/RunList.vue](../frontend/src/views/api-testing/RunList.vue#L1) | ReportList.vue | 已改 | 通过 | 待验 |
| [views/fastbot/FastbotDashboard.vue](../frontend/src/views/fastbot/FastbotDashboard.vue#L1) | Fastbot.vue | 已改 | 通过 | 待验 |
| [views/fastbot/components/CrashEventsCard.vue](../frontend/src/views/fastbot/components/CrashEventsCard.vue#L1) | FastbotReportDetail.vue | 已改 | 通过 | 待验 |
| [views/fastbot/components/JankChartCard.vue](../frontend/src/views/fastbot/components/JankChartCard.vue#L1) | FastbotReportDetail.vue | 已改 | 通过 | 待验 |
| [views/fastbot/components/JankEventsCard.vue](../frontend/src/views/fastbot/components/JankEventsCard.vue#L1) | FastbotReportDetail.vue | 已改 | 通过 | 待验 |
| [views/fastbot/components/LogAnalysisDialog.vue](../frontend/src/views/fastbot/components/LogAnalysisDialog.vue#L1) | FastbotReportDetail.vue | 已改 | 通过 | 待验 |
| [views/fastbot/components/PerformanceChartCard.vue](../frontend/src/views/fastbot/components/PerformanceChartCard.vue#L1) | FastbotReportDetail.vue | 已改 | 通过 | 待验 |
| [views/fastbot/components/ReplayDialog.vue](../frontend/src/views/fastbot/components/ReplayDialog.vue#L1) | FastbotReportDetail.vue | 已改 | 通过 | 待验 |
| [views/fastbot/components/StartupReportSection.vue](../frontend/src/views/fastbot/components/StartupReportSection.vue#L1) | FastbotReportDetail.vue | 已改 | 通过 | 待验 |
| [views/fastbot/components/StatCard.vue](../frontend/src/views/fastbot/components/StatCard.vue#L1) | FastbotReportDetail.vue、StartupReportSection.vue | 已改 | 通过 | 待验 |
| [views/fastbot/components/TraceAiDialog.vue](../frontend/src/views/fastbot/components/TraceAiDialog.vue#L1) | FastbotReportDetail.vue | 已改 | 通过 | 待验 |
| [views/fastbot/components/TraceArtifactsCard.vue](../frontend/src/views/fastbot/components/TraceArtifactsCard.vue#L1) | FastbotReportDetail.vue | 已改 | 通过 | 待验 |
| [views/reports/ExecutionCompareDialog.vue](../frontend/src/views/reports/ExecutionCompareDialog.vue#L1) | ReportDetail.vue | 已改 | 通过 | 待验 |
| [views/reports/FlakyAnalysisDrawer.vue](../frontend/src/views/reports/FlakyAnalysisDrawer.vue#L1) | ReportList.vue | 已改 | 通过 | 待验 |

### 未改文件与遗留组件审计

- `views/special/Fastbot.vue` 仅7行封装，直接渲染已改 `FastbotDashboard.vue`，没有独立样式、状态或操作，不是遗漏。
- `layout/components/SidebarMenuItem.vue` 仅递归菜单/权限过滤，没有局部style；由布局侧栏深度样式及Element Plus tokens统一。需随导航验收递归层级、菜单选中和键盘操作。
- [components/CaseExplorer.vue](../frontend/src/components/CaseExplorer.vue#L1)：源码无路由/活跃import入口，已同步基础主题但不计活跃界面验收，不新增入口。
- [components/StepList.vue](../frontend/src/components/StepList.vue#L1)：源码无路由/活跃import入口，已同步基础主题但不计活跃界面验收，不新增入口。
- [components/VariablePanel.vue](../frontend/src/components/VariablePanel.vue#L1)：源码无路由/活跃import入口，已同步基础主题但不计活跃界面验收，不新增入口。

## Dialog、Drawer与Popover逐定义清单

基线（Git HEAD）：38 Dialog + 11 Drawer + 2 Popover。当前：38 Dialog + 14 Drawer + 7 Popover。新增3个Drawer分别为用例添加动作、接口资产信息、报告批次设备结果；新增5处列表元数据Popover。原生 ElMessageBox 确认框保留并继承全局主题，不计入组件定义数。

| 类型/序号 | 定义位置 | 绑定/标题 | 实现与保留交互 | 编译 | 浏览器视觉 |
|---|---|---|---|---|---|
| dialog 1 | [components/DeviceStage.vue](../frontend/src/components/DeviceStage.vue#L822) | `linkDiagVisible · 链路诊断（本次后端运行期累计）` | 已改；滚动/确认/禁用逻辑保留 | 通过 | 待验 |
| dialog 2 | [components/StepBuilder.vue](../frontend/src/components/StepBuilder.vue#L610) | `aiDialogVisible · AI 智能生成测试步骤` | 已改；滚动/确认/禁用逻辑保留 | 通过 | 待验 |
| dialog 3 | [components/api-testing/AddToScenarioDialog.vue](../frontend/src/components/api-testing/AddToScenarioDialog.vue#L15) | `modelValue · 加入场景` | 已改；滚动/确认/禁用逻辑保留 | 通过 | 待验 |
| dialog 4 | [components/api-testing/AiAssertions.vue](../frontend/src/components/api-testing/AiAssertions.vue#L71) | `open · AI 校验建议` | 已改；滚动/确认/禁用逻辑保留 | 通过 | 待验 |
| drawer 1 | [components/api-testing/AiFailureExplanation.vue](../frontend/src/components/api-testing/AiFailureExplanation.vue#L24) | `open · AI 失败解释` | 已改；滚动/确认/禁用逻辑保留 | 通过 | 待验 |
| dialog 5 | [components/api-testing/AssertionsEditor.vue](../frontend/src/components/api-testing/AssertionsEditor.vue#L84) | `suggestionOpen · 添加业务校验` | 已改；滚动/确认/禁用逻辑保留 | 通过 | 待验 |
| dialog 6 | [components/api-testing/CurlImportDialog.vue](../frontend/src/components/api-testing/CurlImportDialog.vue#L21) | `modelValue · 粘贴 cURL` | 已改；滚动/确认/禁用逻辑保留 | 通过 | 待验 |
| drawer 2 | [components/api-testing/EnvironmentDrawer.vue](../frontend/src/components/api-testing/EnvironmentDrawer.vue#L78) | `modelValue · 环境与变量` | 已改；滚动/确认/禁用逻辑保留 | 通过 | 待验 |
| dialog 7 | [components/api-testing/EnvironmentDrawer.vue](../frontend/src/components/api-testing/EnvironmentDrawer.vue#L85) | `envDialog · editingEnv?'编辑环境':'新建环境'` | 已改；滚动/确认/禁用逻辑保留 | 通过 | 待验 |
| dialog 8 | [components/api-testing/EnvironmentDrawer.vue](../frontend/src/components/api-testing/EnvironmentDrawer.vue#L86) | `variableDialog · editingVariable?'编辑变量':'添加变量'` | 已改；滚动/确认/禁用逻辑保留 | 通过 | 待验 |
| dialog 9 | [components/api-testing/ReferencePicker.vue](../frontend/src/components/api-testing/ReferencePicker.vue#L30) | `modelValue · fieldOnly ? '选择响应字段' : allowStepReferences ? '插入引用' : '选择环境变量'` | 已改；滚动/确认/禁用逻辑保留 | 通过 | 待验 |
| dialog 10 | [components/api-testing/SaveInterfaceDialog.vue](../frontend/src/components/api-testing/SaveInterfaceDialog.vue#L14) | `modelValue · 另存到接口库` | 已改；滚动/确认/禁用逻辑保留 | 通过 | 待验 |
| drawer 3 | [components/api-testing/ScheduleDrawer.vue](../frontend/src/components/api-testing/ScheduleDrawer.vue#L23) | `modelValue · 定时与通知` | 已改；滚动/确认/禁用逻辑保留 | 通过 | 待验 |
| popover 1 | [components/api-testing/ValueEditor.vue](../frontend/src/components/api-testing/ValueEditor.vue#L42) | `行内详情/引用` | 点击触发、信息保留；键盘入口 | 通过 | 待验 |
| popover 2 | [components/api-testing/ValueEditor.vue](../frontend/src/components/api-testing/ValueEditor.vue#L50) | `行内详情/引用` | 点击触发、信息保留；键盘入口 | 通过 | 待验 |
| dialog 11 | [components/api-testing/ValueEditor.vue](../frontend/src/components/api-testing/ValueEditor.vue#L62) | `jsonDialog · 粘贴 JSON` | 已改；滚动/确认/禁用逻辑保留 | 通过 | 待验 |
| dialog 12 | [views/admin/UserManagement.vue](../frontend/src/views/admin/UserManagement.vue#L227) | `createDialogVisible · 新增用户` | 已改；滚动/确认/禁用逻辑保留 | 通过 | 待验 |
| drawer 4 | [views/api-testing/AssetList.vue](../frontend/src/views/api-testing/AssetList.vue#L131) | `detailsOpen · 资产信息` | 已改；滚动/确认/禁用逻辑保留 | 通过 | 待验 |
| dialog 13 | [views/api-testing/AssetList.vue](../frontend/src/views/api-testing/AssetList.vue#L132) | `runDialog · `运行 · ${runItem?.name\|\|'场景'}`` | 已改；滚动/确认/禁用逻辑保留 | 通过 | 待验 |
| drawer 5 | [views/api-testing/InterfaceEditor.vue](../frontend/src/views/api-testing/InterfaceEditor.vue#L114) | `infoOpen · 接口信息` | 已改；滚动/确认/禁用逻辑保留 | 通过 | 待验 |
| dialog 14 | [views/api-testing/InterfaceEditor.vue](../frontend/src/views/api-testing/InterfaceEditor.vue#L115) | `sampleDialog · 响应体 JSON 样例` | 已改；滚动/确认/禁用逻辑保留 | 通过 | 待验 |
| drawer 6 | [views/api-testing/ScenarioEditor.vue](../frontend/src/views/api-testing/ScenarioEditor.vue#L212) | `libraryOpen · 添加步骤` | 已改；滚动/确认/禁用逻辑保留 | 通过 | 待验 |
| drawer 7 | [views/api-testing/ScenarioEditor.vue](../frontend/src/views/api-testing/ScenarioEditor.vue#L221) | `infoOpen · 场景信息` | 已改；滚动/确认/禁用逻辑保留 | 通过 | 待验 |
| dialog 15 | [views/api-testing/ScenarioEditor.vue](../frontend/src/views/api-testing/ScenarioEditor.vue#L224) | `runOptions · 仅本次运行设置` | 已改；滚动/确认/禁用逻辑保留 | 通过 | 待验 |
| dialog 16 | [views/api-testing/ScenarioEditor.vue](../frontend/src/views/api-testing/ScenarioEditor.vue#L225) | `syncDialog · 同步接口变更` | 已改；滚动/确认/禁用逻辑保留 | 通过 | 待验 |
| dialog 17 | [views/api-testing/ScenarioEditor.vue](../frontend/src/views/api-testing/ScenarioEditor.vue#L226) | `authOpen · 应用到后续步骤的鉴权` | 已改；滚动/确认/禁用逻辑保留 | 通过 | 待验 |
| drawer 8 | [views/cases/CaseEditor.vue](../frontend/src/views/cases/CaseEditor.vue#L400) | `actionsVisible · 添加通用动作` | 已改；滚动/确认/禁用逻辑保留 | 通过 | 待验 |
| dialog 18 | [views/cases/CaseEditor.vue](../frontend/src/views/cases/CaseEditor.vue#L405) | `runDialogVisible · 选择多设备执行` | 已改；滚动/确认/禁用逻辑保留 | 通过 | 待验 |
| popover 3 | [views/cases/CaseList.vue](../frontend/src/views/cases/CaseList.vue#L679) | `行内详情/引用` | 点击触发、信息保留；键盘入口 | 通过 | 待验 |
| dialog 19 | [views/cases/CaseList.vue](../frontend/src/views/cases/CaseList.vue#L707) | `runDialogVisible · 运行配置` | 已改；滚动/确认/禁用逻辑保留 | 通过 | 待验 |
| drawer 9 | [views/cases/CaseList.vue](../frontend/src/views/cases/CaseList.vue#L753) | `runDialogVisible · 运行配置` | 已改；滚动/确认/禁用逻辑保留 | 通过 | 待验 |
| dialog 20 | [views/devices/DeviceCenter.vue](../frontend/src/views/devices/DeviceCenter.vue#L561) | `screenshotVisible · `实时屏幕快照 — ${screenshotDevice?.model \|\| ''}`` | 已改；滚动/确认/禁用逻辑保留 | 通过 | 待验 |
| dialog 21 | [views/devices/DeviceCenter.vue](../frontend/src/views/devices/DeviceCenter.vue#L786) | `screenshotVisible · `实时屏幕快照 — ${screenshotDevice?.model \|\| ''}`` | 已改；滚动/确认/禁用逻辑保留 | 通过 | 待验 |
| dialog 22 | [views/devices/DeviceCenter.vue](../frontend/src/views/devices/DeviceCenter.vue#L829) | `agentGuideVisible · 接入远程设备（设备插在自己电脑上使用平台）` | 已改；滚动/确认/禁用逻辑保留 | 通过 | 待验 |
| dialog 23 | [views/fastbot/FastbotDashboard.vue](../frontend/src/views/fastbot/FastbotDashboard.vue#L311) | `deviceDialogVisible · 选择设备` | 已改；滚动/确认/禁用逻辑保留 | 通过 | 待验 |
| dialog 24 | [views/fastbot/components/LogAnalysisDialog.vue](../frontend/src/views/fastbot/components/LogAnalysisDialog.vue#L92) | `visible · `${logEventType} 日志快照`` | 已改；滚动/确认/禁用逻辑保留 | 通过 | 待验 |
| dialog 25 | [views/fastbot/components/ReplayDialog.vue](../frontend/src/views/fastbot/components/ReplayDialog.vue#L28) | `visible · currentReplayTitle` | 已改；滚动/确认/禁用逻辑保留 | 通过 | 待验 |
| dialog 26 | [views/fastbot/components/TraceAiDialog.vue](../frontend/src/views/fastbot/components/TraceAiDialog.vue#L72) | `visible · Perfetto Trace AI 总结` | 已改；滚动/确认/禁用逻辑保留 | 通过 | 待验 |
| dialog 27 | [views/packages/PackageManagement.vue](../frontend/src/views/packages/PackageManagement.vue#L391) | `uploadDialogVisible · 安装包上传` | 已改；滚动/确认/禁用逻辑保留 | 通过 | 待验 |
| dialog 28 | [views/packages/PackageManagement.vue](../frontend/src/views/packages/PackageManagement.vue#L518) | `installDialogVisible · installTarget?.platform === 'ios' ? '安装 IPA 到指定 iPhone' : '推送到指定设备'` | 已改；滚动/确认/禁用逻辑保留 | 通过 | 待验 |
| drawer 10 | [views/reports/CompatibilityReportDetail.vue](../frontend/src/views/reports/CompatibilityReportDetail.vue#L649) | `resultDrawerVisible · isInstalledReplay ? '链路回放详情' : '页面对比详情'` | 已改；滚动/确认/禁用逻辑保留 | 通过 | 待验 |
| dialog 29 | [views/reports/ExecutionCompareDialog.vue](../frontend/src/views/reports/ExecutionCompareDialog.vue#L110) | `visible · 执行结果对比` | 已改；滚动/确认/禁用逻辑保留 | 通过 | 待验 |
| drawer 11 | [views/reports/FlakyAnalysisDrawer.vue](../frontend/src/views/reports/FlakyAnalysisDrawer.vue#L56) | `visible · 稳定性分析（Flaky Top）` | 已改；滚动/确认/禁用逻辑保留 | 通过 | 待验 |
| drawer 12 | [views/reports/InspectionReportDetail.vue](../frontend/src/views/reports/InspectionReportDetail.vue#L1910) | `drawerVisible · drawerTitle` | 已改；滚动/确认/禁用逻辑保留 | 通过 | 待验 |
| dialog 30 | [views/reports/ReportDetail.vue](../frontend/src/views/reports/ReportDetail.vue#L401) | `showScreenshot · currentPreviewTitle` | 已改；滚动/确认/禁用逻辑保留 | 通过 | 待验 |
| dialog 31 | [views/reports/ReportDetail.vue](../frontend/src/views/reports/ReportDetail.vue#L552) | `showScreenshot · currentPreviewTitle` | 已改；滚动/确认/禁用逻辑保留 | 通过 | 待验 |
| popover 4 | [views/reports/ReportList.vue](../frontend/src/views/reports/ReportList.vue#L975) | `行内详情/引用` | 点击触发、信息保留；键盘入口 | 通过 | 待验 |
| popover 5 | [views/reports/ReportList.vue](../frontend/src/views/reports/ReportList.vue#L1059) | `行内详情/引用` | 点击触发、信息保留；键盘入口 | 通过 | 待验 |
| popover 6 | [views/reports/ReportList.vue](../frontend/src/views/reports/ReportList.vue#L1159) | `行内详情/引用` | 点击触发、信息保留；键盘入口 | 通过 | 待验 |
| drawer 13 | [views/reports/ReportList.vue](../frontend/src/views/reports/ReportList.vue#L1307) | `batchDrawerVisible · selectedBatch?.batch_name \|\| '设备执行结果'` | 已改；滚动/确认/禁用逻辑保留 | 通过 | 待验 |
| dialog 32 | [views/scenarios/ScenarioEditor.vue](../frontend/src/views/scenarios/ScenarioEditor.vue#L834) | `runDialogVisible · 选择多设备执行` | 已改；滚动/确认/禁用逻辑保留 | 通过 | 待验 |
| popover 7 | [views/scenarios/ScenarioList.vue](../frontend/src/views/scenarios/ScenarioList.vue#L625) | `行内详情/引用` | 点击触发、信息保留；键盘入口 | 通过 | 待验 |
| dialog 33 | [views/scenarios/ScenarioList.vue](../frontend/src/views/scenarios/ScenarioList.vue#L648) | `runDialogVisible · 运行配置` | 已改；滚动/确认/禁用逻辑保留 | 通过 | 待验 |
| drawer 14 | [views/scenarios/ScenarioList.vue](../frontend/src/views/scenarios/ScenarioList.vue#L694) | `runDialogVisible · 运行配置` | 已改；滚动/确认/禁用逻辑保留 | 通过 | 待验 |
| dialog 34 | [views/settings/ApiTokens.vue](../frontend/src/views/settings/ApiTokens.vue#L185) | `createDialogVisible · 创建 API Token` | 已改；滚动/确认/禁用逻辑保留 | 通过 | 待验 |
| dialog 35 | [views/special/Inspection.vue](../frontend/src/views/special/Inspection.vue#L629) | `dialogVisible · editingId ? '编辑巡检配置' : '新建巡检配置'` | 已改；滚动/确认/禁用逻辑保留 | 通过 | 待验 |
| dialog 36 | [views/tasks/TaskList.vue](../frontend/src/views/tasks/TaskList.vue#L532) | `dialogVisible · dialogTitle` | 已改；滚动/确认/禁用逻辑保留 | 通过 | 待验 |
| dialog 37 | [views/variables/VariableLibrary.vue](../frontend/src/views/variables/VariableLibrary.vue#L316) | `envDialogVisible · editingEnv ? '编辑环境' : '新建环境'` | 已改；滚动/确认/禁用逻辑保留 | 通过 | 待验 |
| dialog 38 | [views/variables/VariableLibrary.vue](../frontend/src/views/variables/VariableLibrary.vue#L337) | `varDialogVisible · editingVar ? '编辑变量' : '新增变量'` | 已改；滚动/确认/禁用逻辑保留 | 通过 | 待验 |

## 报告与长表单的分支覆盖

| 入口 | 必须验收的分支 | 实现 | 行为/编译证据 | 浏览器视觉 |
|---|---|---|---|---|
| 报告中心6个标签 | UI场景、接口、智能探索、冷热启动、兼容性、智能巡检；所有标签空/失败/有结果/运行中 | 已改 | 全域SFC通过；报告Node行为测试 | 待验 |
| UI批次报告 | 单设备直达、多设备结果Drawer、失败证据、下载/终止/删除权限 | 已改 | reportRedesign相关导航行为测试 | 待验 |
| 接口请求编辑 | 请求方法/URL、路径/查询参数、JSON/表单/文本体、请求头、鉴权、高级超时、校验 | 已改 | apiEditors/apiTesting/apiDebug行为测试 | 待验 |
| 接口响应/引用/AI | 树/原文/Headers/Cookie/引用/断言、字段选择、鉴权引用、AI建议/解释、失效结果 | 已改 | apiAi/apiTesting行为测试 | 待验 |
| 系统设置3个标签 | 3种通知配置、AI服务与辅助开关、试验核心/身份/覆盖/视觉、存储与容量 | 已改 | settingsUiContracts 4项通过 | 待验 |
| UI编辑器 | 单/多设备、截图/投屏、元素/OCR/图像框选、动作库、标准步骤、日志 | 已改 | uiRunLifecycle 10项通过 | 待验 |
| 专项配置 | 稳定性/流畅度/启动/兼容性/页面合集/巡检配置/实时面板；基础与高级/错误定位 | 已改 | SFC/相关Node契约通过 | 待验 |
| 专项报告 | CPU/内存/卡顿/启动图表、事件、截图、回放、日志AI、Trace AI、巡检树/动作/状态 | 已改 | ECharts共享主题；SFC/相关报告测试 | 待验 |
| 定时任务 | UI/API、策略、星期/时间/间隔、环境、通知、验证错误、保存失败 | 已改 | 防重/失败重试行为测试 | 待验 |
| 设备/安装包 | Android/iOS、连通/离线/异常/占用、维护、接入、上传/取消/安装 | 已改 | SFC通过；真实链路未测 | 待验 |
| 账号与资产 | 登录/注册关闭/密码错误、管理员/普通用户、Token一次性展示、密文显隐、环境/变量 | 已改 | SFC通过 | 待验 |

## 三套独立HTML报告

| 输出来源 | 改动/兼容 | 自动验证 | 浏览器视觉 |
|---|---|---|---|
| [UI用例模板](../backend/templates/report.html) | 自包含浅色紧凑；失败锚点/展开；保持截图像素；历史归档不重写 | report_redesign/display/assets/filters合计29项通过 | 待验 |
| [UI场景模板](../backend/templates/scenario_report.html) | 自包含浅色紧凑；失败用例锚点、重复case_id不冲突；转义证据 | 同上 | 待验 |
| [接口HTML生成器](../backend/api_testing/reporting.py) | 自包含浅色；失败响应优先、请求与引用折叠；导出接口不变 | 同上 | 待验 |

## 测试与证据登记

| 检查 | 结果 | 限制 |
|---|---|---|
| 全77个Vue的compileScript + compileTemplate + compileStyle | 77/77通过（2026-10-03） | 不等同浏览器布局或交互通过 |
| 接口/设置现有测试与新增apiAssetRuns | 47/47通过 | 生产setup函数+依赖替身；无真实请求 |
| UI运行生命周期新回归 | 10/10通过（UI域代理） | 保存/预检/提交逻辑；无真实设备 |
| 报告/调度新增Node回归 | 14/14通过（报告域代理） | 导航/防重/失败重试 |
| 后端报告渲染/资产/过滤 | 29/29通过（报告域代理） | 模板与数据行为，未调用设备或通知 |
| 生产构建 | 通过（最终整合，2026-10-03） | jmuxer stream外置提示；Element Plus chunk约999kB |
| 完整前端Node测试 | 143/143通过（2026-10-03） | 无真实请求/设备 |
| 后端报告回归 | 29/29通过（`.venv/bin/python -m pytest ... -q`） | 含新模板、资产路径和过滤行为；未调用设备或通知 |
| 隔离预览 API smoke | 9/9通过（TestClient，2026-10-03） | registration-status、feature-flags、dashboard、cases、scenarios、devices、reports、API资产列表均返回200；仅内存样例，不连接真实服务 |
| 移动端静态规格审计 | 通过（2026-10-03） | `body.ad-mobile`统一覆盖14px正文、16px输入、44px主要控件；窄屏局部规则已检查；未替代390px/320px真实浏览器视觉验收 |

### 浏览器证据待填写

| 视口/角色 | 范围 | 截图/记录 | 结果 |
|---|---|---|---|
| 1440×900，100% | 大盘、用例列表、编辑器；标准列表行数 | [dashboard-after-1440.png](ui-redesign/evidence/dashboard-after-1440.png)、[cases-after-1440.png](ui-redesign/evidence/cases-after-1440.png) | 已验：用例表19行、行高36px、无横滚 |
| 1366×768，100% | 用例列表/编辑器、名称/状态/主要操作、底部操作 | [cases-after-1366.png](ui-redesign/evidence/cases-after-1366.png)、[case-editor-after-1366.png](ui-redesign/evidence/case-editor-after-1366.png)、[case-actions-drawer-1366.png](ui-redesign/evidence/case-actions-drawer-1366.png) | 已验：用例表15行、行高36px、动作弹层可达 |
| 1920×1080，100% | 全页布局、编辑区/报告图表 | 待填写 | 待验 |
| 390px，移动 | 原移动能力边界、14/16/44px、滚动与弹层 | 待填写 | 待验 |
| 320px，移动 | 同上及长名称/标签换行 | 待填写 | 待验 |
| 管理员/普通用户，flag开/关 | 用户管理/删除/停用/巡检入口/直链限制 | 待填写 | 待验 |
| Android/iOS真实设备 | 实际执行、维护、上传安装、回放、AI与通知 | 未执行 | 未测；隔离示例仅验证页面状态 |

### 实现细节与已保留约束

- 运行保存：UI基础信息与标准步骤均成功才更新快照；失败保留草稿和已创建ID；新建或修改后保存并运行；预检与提交独立防重、冻结环境/设备。
- API列表运行：抓取完整场景后，预检仅发name/steps/env_id/validation_mode，符合extra=forbid契约；冻结场景ID/version/环境/通知；失败允许重试。
- 未修改业务API/数据库/执行协议；代理匹配收窄以避免/api-testing页面刷新误代理。
- ECharts消费共享主题；视频/截图/设备画面原始像素不重染。
- 删除、吊销、停用、停止、覆盖及设备维护确认保留；未发送真实消息。
- 桌面抽样验收证实了密度目标和编辑器结构；其余路由/弹层仍以源码、SFC编译和行为测试为证据，不能写成已完成视觉验收。
