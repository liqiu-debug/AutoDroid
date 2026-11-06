import assert from 'node:assert/strict'
import test from 'node:test'
import { h, ref } from 'vue'

import { createProgressNotice } from '../src/utils/progressNotice.js'

// 真实 ElNotification：每次调用新开一个通知，同 id 不替换；句柄只有 close()。
const fakeNotify = () => {
  const opened = []
  const notify = options => {
    const entry = { options, closed: false }
    opened.push(entry)
    return { close: () => { entry.closed = true; options.onClose?.() } }
  }
  return { notify, opened }
}

test('many progress updates open exactly one notification and re-render its text', () => {
  const { notify, opened } = fakeNotify()
  const notice = createProgressNotice({ notify, h, ref, title: '下载更新', message: '正在下载：0%' })
  for (let pct = 1; pct <= 100; pct += 1) notice.update(`正在下载：${pct}%`)
  assert.equal(opened.length, 1)
  const vnode = opened[0].options.message()
  assert.equal(vnode.children, '正在下载：100%')
  assert.equal(opened[0].options.duration, 0)
  notice.close()
  notice.close()
  assert.equal(opened[0].closed, true)
})

test('a notice the user dismissed is not reopened by later progress', () => {
  const { notify, opened } = fakeNotify()
  const notice = createProgressNotice({ notify, h, ref, title: 't', message: 'a' })
  opened[0].options.onClose()
  notice.update('b')
  notice.close()
  assert.equal(opened.length, 1)
  assert.equal(notice.closed, true)
  assert.equal(opened[0].closed, false, '已被用户关闭，不再重复 close')
})
