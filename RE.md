全栈开发设计方案：新增 Sleep 动作
一、 后端基建改造 (Backend)
1. 修改枚举定义 (backend/schemas.py)
在 ActionType 枚举中新增 sleep 动作。

Python
class ActionType(str, Enum):
    # ... 现有动作
    SLEEP = "sleep"  # 🟢 新增：强制等待/睡眠
2. 确立 sleep 的数据契约
当 action 为 sleep 时，标准 JSON 应该长这样：

value: 存放秒数（字符串格式，如 "5"）。

selector, selector_type: null（不需要查找元素）。

3. 运行引擎适配 (backend/runner.py)
在实际执行步骤的引擎逻辑中，增加对 sleep 的拦截处理：

Python
import time

# 在处理执行动作的 switch/if-else 逻辑中：
if step.action == ActionType.SLEEP:
    if not step.value or not step.value.isdigit():
        raise ValueError("sleep 动作的 value 必须提供有效的秒数 (整数)")
    sleep_seconds = int(step.value)
    # print(f"休眠等待 {sleep_seconds} 秒...")
    time.sleep(sleep_seconds)
    return True
二、 前端页面改造 (StepBuilder.vue)
你需要修改 Vue 页面，确保用户能在左侧拖拽、在右侧编辑。

1. 左侧：通用步骤列表 (Draggable Source)
在你存放“通用步骤”的数组（通常叫 commonSteps 或直接写在 HTML 里的列表）中，增加一个等待模块：

HTML
<div class="common-step-item" draggable="true" @dragstart="handleDragStart('sleep')">
  <el-icon><Timer /></el-icon> 等待 (Sleep)
</div>

{
  action: 'sleep',
  selector: null,
  selector_type: null,
  value: '3', // 默认给 3 秒
  description: '强制等待 3 秒',
  timeout: 10,
  error_strategy: 'ABORT'
}
2. 右侧：步骤卡片渲染 (Step Card UI)
修改 <div v-for="step in currentCase.steps"> 内部的渲染逻辑。当动作为 sleep 时，隐藏“定位方式”和“选择器”，显示一个专属的“等待时间”输入框。

HTML
<div v-if="['input', 'assert_text', 'sleep'].includes(step.action)">
  <label v-if="step.action === 'sleep'">等待时间 (秒)</label>
  <label v-else>值</label>
  
  <el-input-number 
    v-if="step.action === 'sleep'" 
    v-model="step.value" 
    :min="1" 
    :max="60" 
    controls-position="right"
    style="width: 120px;"
  />
  <el-input v-else v-model="step.value" />
</div>

三、 AI 生成引擎无缝接入 (NL2Script)
这是最神奇的部分。因为我们之前设计了**“动态 Prompt”**，后端代码会自动把新加的 sleep 加载到可用动作列表中。我们只需要在 api/ai_script.py 的 System Prompt 里，教大模型如何处理时间参数即可。

打开你刚才写的 generate_nl2script 函数，在 【⚠️ 核心动作特例规则】 的部分，追加第 5 条规则：

Python
# 修改 backend/api/ai_script.py 的 system_prompt 字符串：

system_prompt = f"""
... (前面的保持不变) ...

【⚠️ 核心动作特例规则 (极其重要)】
1. 【输入与断言】(action: input / assert_text):
   - value 字段必须填写要输入或断言的文本内容。

2. 【滑动】(action: swipe):
   - selector 字段必须填写方向 ("up", "down", "left", "right")，其余为 null。
   
3. 【应用启停】(action: start_app / stop_app):
   - selector 字段必须填写包名，其余为 null。
   
4. 【全局按键】(action: back / home):
   - selector, selector_type, value 必须全部为 null。

5. 🌟【等待/睡眠】(action: sleep):
   - 当用户要求“等待”、“停留”、“休眠”多少秒时使用。
   - value 字段必须只填写纯数字字符串（代表秒数，如 "5"）。
   - selector 和 selector_type 必须为 null。

... (后面的保持不变) ...
