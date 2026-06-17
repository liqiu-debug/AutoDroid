<script setup>
import { computed } from 'vue'

defineOptions({ name: 'SidebarMenuItem' })

const props = defineProps({
  item: {
    type: Object,
    required: true,
  },
  parentPath: {
    type: String,
    default: '',
  },
  depth: {
    type: Number,
    default: 0,
  },
  canShowRoute: {
    type: Function,
    required: true,
  },
})

const joinPath = (...parts) => {
  const normalized = parts
    .filter(part => part !== undefined && part !== null && String(part).length > 0)
    .map(part => String(part).replace(/^\/+|\/+$/g, ''))
    .filter(Boolean)
  return `/${normalized.join('/')}`.replace(/\/+/g, '/')
}

const visibleChildren = computed(() => (
  (props.item.children || []).filter(child => !child.meta?.hidden && props.canShowRoute(child))
))

const itemPath = computed(() => {
  if (String(props.item.path || '').startsWith('/')) return props.item.path
  return joinPath(props.parentPath, props.item.path)
})

const leafPath = computed(() => {
  if (visibleChildren.value.length === 1 && !props.item.meta?.alwaysShow) {
    const child = visibleChildren.value[0]
    if (String(child.path || '').startsWith('/')) return child.path
    return joinPath(itemPath.value, child.path)
  }
  return itemPath.value
})

const shouldRenderSubMenu = computed(() => visibleChildren.value.length > 1 || props.item.meta?.alwaysShow)
</script>

<template>
  <el-sub-menu v-if="shouldRenderSubMenu" :index="itemPath">
    <template #title>
      <el-icon v-if="item.meta?.icon && depth === 0">
        <component :is="item.meta.icon" />
      </el-icon>
      <span>{{ item.meta?.title }}</span>
    </template>
    <SidebarMenuItem
      v-for="child in visibleChildren"
      :key="`${itemPath}/${child.path}`"
      :item="child"
      :parent-path="itemPath"
      :depth="depth + 1"
      :can-show-route="canShowRoute"
    />
  </el-sub-menu>

  <el-menu-item v-else :index="leafPath">
    <el-icon v-if="item.meta?.icon && depth === 0">
      <component :is="item.meta.icon" />
    </el-icon>
    <template #title>{{ item.meta?.title }}</template>
  </el-menu-item>
</template>
