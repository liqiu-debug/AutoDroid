<script setup>
import { Monitor } from '@element-plus/icons-vue'
import { useClientMode } from '@/composables/useClientMode'

const { setMode } = useClientMode()
</script>

<template>
  <div class="mobile-unavailable">
    <div class="notice-icon">
      <el-icon><Monitor /></el-icon>
    </div>
    <h2>该功能仅支持 PC 模式</h2>
    <p>移动端保留查看和轻量执行能力，资产编辑、系统配置和复杂编排请切换到 PC 模式使用。</p>
    <el-button type="primary" @click="setMode('pc')">切换 PC 模式</el-button>
  </div>
</template>

<style scoped>
.mobile-unavailable {
  min-height: 100%;
  padding: 48px 22px;
  box-sizing: border-box;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  text-align: center;
  color: #303133;
  background: #f6f7f9;
}

.notice-icon {
  width: 54px;
  height: 54px;
  border-radius: 50%;
  display: flex;
  align-items: center;
  justify-content: center;
  color: #409eff;
  background: #ecf5ff;
  font-size: 26px;
  margin-bottom: 16px;
}

.mobile-unavailable h2 {
  margin: 0 0 8px;
  font-size: 20px;
}

.mobile-unavailable p {
  margin: 0 0 22px;
  max-width: 320px;
  color: #606266;
  font-size: 14px;
  line-height: 1.6;
}
</style>
