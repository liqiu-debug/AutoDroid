/**
 * 单个可原地更新的进度通知（下载进度等高频事件用）。
 *
 * 为什么需要它：Element Plus 的 ElNotification 每次调用都会新开一个通知，传同一个 id
 * 也不会替换旧的（id 只是 DOM id）；而在返回的句柄上改 message 也不会触发重绘。
 * 下载进度每个事件都调一次 ElNotification，结果就是一屏叠满「正在下载」。
 *
 * 做法：只打开一次通知，message 用渲染函数读一个响应式文本，update() 只改文本。
 * 用户手动关掉后不再重新弹出（update 静默生效，close 幂等）。
 */
export function createProgressNotice({ notify, h, ref, title, message, type = 'info' }) {
  const text = ref(String(message ?? ''))
  let closed = false
  const handle = notify({
    title,
    type,
    duration: 0,
    message: () => h('span', text.value),
    onClose: () => {
      closed = true
    },
  })
  return {
    get text() {
      return text.value
    },
    get closed() {
      return closed
    },
    update(next) {
      text.value = String(next ?? '')
    },
    close() {
      if (closed) return
      closed = true
      handle?.close?.()
    },
  }
}
