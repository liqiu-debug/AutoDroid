import { ref, onBeforeUnmount } from 'vue'

/**
 * 目录面板宽度可拖拽调整（用例/场景/接口目录共用）。
 * 默认 260px，拖拽范围 200–480px，宽度按 localStorage 持久化，重启保留。
 * 拖拽逻辑集中在本 composable：各页面只消费 { width, startResize }，避免复制粘贴。
 */

export const ASIDE_WIDTH_STORAGE_KEY = 'autodroid.folder_aside_width'
export const ASIDE_DEFAULT_WIDTH = 260
export const ASIDE_MIN_WIDTH = 200
export const ASIDE_MAX_WIDTH = 480

/** 把任意输入收敛到 [min, max]；非法值退回 fallback（默认 260）。 */
export function clampAsideWidth(value, min = ASIDE_MIN_WIDTH, max = ASIDE_MAX_WIDTH, fallback = ASIDE_DEFAULT_WIDTH) {
    const lower = Math.min(min, max)
    const upper = Math.max(min, max)
    const width = Math.round(Number(value))
    const safe = Number.isFinite(width) ? width : Math.round(Number(fallback))
    const base = Number.isFinite(safe) ? safe : ASIDE_DEFAULT_WIDTH
    return Math.min(upper, Math.max(lower, base))
}

/** 读取持久化宽度；缺失或非法时返回默认值。 */
export function readAsideWidth(storage, storageKey = ASIDE_WIDTH_STORAGE_KEY, min = ASIDE_MIN_WIDTH, max = ASIDE_MAX_WIDTH) {
    let raw = null
    try {
        raw = storage?.getItem?.(storageKey) ?? null
    } catch {
        raw = null
    }
    return clampAsideWidth(raw === null || raw === '' ? NaN : raw, min, max)
}

/** 持久化宽度；隐私模式等写入失败静默忽略，不影响拖拽。 */
export function writeAsideWidth(storage, storageKey = ASIDE_WIDTH_STORAGE_KEY, value = ASIDE_DEFAULT_WIDTH) {
    try {
        storage?.setItem?.(storageKey, String(value))
    } catch {
        /* 忽略写入失败 */
    }
}

export function useResizableAside(options = {}) {
    const storageKey = options.storageKey || ASIDE_WIDTH_STORAGE_KEY
    const min = options.min ?? ASIDE_MIN_WIDTH
    const max = options.max ?? ASIDE_MAX_WIDTH
    const storage = 'storage' in options
        ? options.storage
        : (typeof localStorage === 'undefined' ? null : localStorage)
    const doc = 'document' in options
        ? options.document
        : (typeof document === 'undefined' ? null : document)

    const width = ref(readAsideWidth(storage, storageKey, min, max))
    const clamp = (value) => clampAsideWidth(value, min, max, width.value)

    let detach = null
    const stopResize = () => {
        if (!detach) return
        detach()
        detach = null
    }

    const startResize = (event) => {
        if (!doc || (event && typeof event.button === 'number' && event.button !== 0)) return
        event?.preventDefault?.()
        stopResize()
        const startX = event?.clientX ?? 0
        const startWidth = width.value
        const onMove = (moveEvent) => {
            width.value = clamp(startWidth + (moveEvent.clientX - startX))
        }
        const onUp = () => {
            stopResize()
            writeAsideWidth(storage, storageKey, width.value)
        }
        doc.addEventListener('mousemove', onMove)
        doc.addEventListener('mouseup', onUp)
        // 拖动期间禁止选中文本/继承拖拽光标
        const bodyStyle = doc.body?.style
        const previous = bodyStyle ? { userSelect: bodyStyle.userSelect, cursor: bodyStyle.cursor } : null
        if (bodyStyle) {
            bodyStyle.userSelect = 'none'
            bodyStyle.cursor = 'col-resize'
        }
        detach = () => {
            doc.removeEventListener('mousemove', onMove)
            doc.removeEventListener('mouseup', onUp)
            if (bodyStyle && previous) {
                bodyStyle.userSelect = previous.userSelect
                bodyStyle.cursor = previous.cursor
            }
        }
    }

    onBeforeUnmount(() => {
        const wasDragging = !!detach
        stopResize()
        if (wasDragging) writeAsideWidth(storage, storageKey, width.value)
    })

    return { width, startResize }
}
