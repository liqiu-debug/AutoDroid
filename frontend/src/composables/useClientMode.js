import { computed, ref } from 'vue'

export const CLIENT_MODE_KEY = 'clientMode'
export const CLIENT_MODE_OPTIONS = ['auto', 'pc', 'mobile']

const mode = ref(readStoredMode())
const viewportWidth = ref(typeof window === 'undefined' ? 1024 : window.innerWidth)
const hasCoarsePointer = ref(false)
let initialized = false
let pointerMediaQuery = null

function readStoredMode() {
  if (typeof window === 'undefined') return 'auto'
  const stored = window.localStorage.getItem(CLIENT_MODE_KEY)
  return CLIENT_MODE_OPTIONS.includes(stored) ? stored : 'auto'
}

function updateSignals() {
  if (typeof window === 'undefined') return
  viewportWidth.value = window.innerWidth
  const coarseQuery = pointerMediaQuery || window.matchMedia?.('(pointer: coarse)')
  hasCoarsePointer.value = Boolean(coarseQuery?.matches)
}

function initClientModeSignals() {
  if (initialized || typeof window === 'undefined') return
  initialized = true
  pointerMediaQuery = window.matchMedia?.('(pointer: coarse)')
  updateSignals()
  window.addEventListener('resize', updateSignals, { passive: true })
  pointerMediaQuery?.addEventListener?.('change', updateSignals)
}

export function useClientMode() {
  initClientModeSignals()

  const isAutoMobile = computed(() => viewportWidth.value <= 768 || hasCoarsePointer.value)
  const effectiveMode = computed(() => {
    if (mode.value === 'pc') return 'pc'
    if (mode.value === 'mobile') return 'mobile'
    return isAutoMobile.value ? 'mobile' : 'pc'
  })
  const isMobileMode = computed(() => effectiveMode.value === 'mobile')

  const setMode = (nextMode) => {
    const safeMode = CLIENT_MODE_OPTIONS.includes(nextMode) ? nextMode : 'auto'
    mode.value = safeMode
    if (typeof window !== 'undefined') {
      window.localStorage.setItem(CLIENT_MODE_KEY, safeMode)
      updateSignals()
    }
  }

  return {
    mode,
    effectiveMode,
    isMobileMode,
    setMode,
  }
}
