/**
 * useWebCodecsDecoder - 基于 WebCodecs VideoDecoder 的 scrcpy H.264 解码管线
 *
 * 输入：push() 一条后端 WS 二进制消息（裸 Annex-B H.264 包，SPS/PPS 为独立 config 包；
 *       后端保证拥塞恢复 / 新客户端接入时序列以 SPS/PPS → IDR 开头）。
 * 输出：解码帧经 requestAnimationFrame 以"最新帧优先"绘制到传入的 canvas，
 *       过期帧立即 close() 释放，避免 VideoFrame 泄漏与延迟累积。
 *
 * 配置策略（依据 W3C WebCodecs AVC Registration）：
 * - configure 不带 description → 码流按 Annex-B 格式解析，与后端契约零转封装；
 * - 规范要求 annexb 的 key chunk "contain both an IDR picture and all parameter
 *   sets necessary"，且每个 chunk 必须是恰含一个 primary coded picture 的 access
 *   unit —— 因此 SPS/PPS config 包只做缓存（绝不单独作为 chunk 喂入），并在每个
 *   key chunk 前拼接缓存的 SPS/PPS；
 * - SPS/PPS 字节发生变化（旋转/重编码）时在下一个 IDR 处 reset() + configure()，
 *   字节未变的重播种（拥塞恢复）则无需重配，IDR 本身即可刷新参考帧状态。
 *
 * 时间戳：无 PTS 透传，按 fpsHint 自造单调递增微秒时间戳（同 jmuxer 的 fps hint 思路）。
 *
 * 页面隐藏行为：与 jmuxer 现状一致 —— 不暂停解码，持续消费 WS 流；隐藏期间 rAF
 * 不触发，仅保留最新一帧（旧帧即时释放），恢复可见后立即绘出最新画面（天然追帧）。
 */
import {
  classifyAnnexBPacket,
  codecStringFromSps,
  buildAnnexBStream,
  bytesEqual
} from '@/utils/h264'

const MICROS_PER_SECOND = 1e6
/** 支持性探测期间的入包缓冲上限（约 5s @60fps，防异常悬挂时内存无界） */
const MAX_PENDING_PACKETS = 300
/** 连续成功输出多少帧后清零"连续错误"计数 */
const STABLE_OUTPUTS_TO_RESET_ERRORS = 30

/** isConfigSupported 结果按 codec 串缓存（同一浏览器会话内结果恒定，避免降级后反复探测） */
const codecSupportCache = new Map()

/**
 * @param {Object} options
 * @param {HTMLCanvasElement} options.canvas 渲染目标画布
 * @param {number} [options.fpsHint=60] 自造时间戳的帧率提示
 * @param {number} [options.maxConsecutiveErrors=3] 连续解码失败阈值，达到后触发 onFallback
 * @param {number} [options.maxQueuedDecodes=60] decodeQueueSize 积压阈值，超出则丢弃至下一个 IDR
 * @param {Function} [options.onFrameDecoded] 每解出一帧回调（用于 FPS 计数）
 * @param {Function} [options.onFallback] 不可恢复时回调（codec 不支持 / 连续解码失败），入参为原因串
 * @param {boolean} [options.debug=false] 开发模式统计日志（console.debug，每秒一条）
 */
export function useWebCodecsDecoder({
  canvas,
  fpsHint = 60,
  maxConsecutiveErrors = 3,
  maxQueuedDecodes = 60,
  onFrameDecoded = null,
  onFallback = null,
  debug = false
} = {}) {
  const frameDurationUs = Math.max(1, Math.round(MICROS_PER_SECOND / fpsHint))
  const ctx = canvas ? canvas.getContext('2d', { alpha: false, desynchronized: true }) : null

  let destroyed = false
  let fellBack = false
  let decoder = null

  // 码流配置状态
  let spsNal = null
  let ppsNal = null
  let needsConfigure = true
  let awaitingKeyFrame = true

  // isConfigSupported 异步探测期间的入包缓冲
  let checkingSupport = false
  let pendingPackets = []

  let timestampUs = 0
  let consecutiveErrors = 0
  let outputsSinceError = 0

  // 渲染：最新帧优先（latest-frame-wins）
  let pendingFrame = null
  let rafId = null

  // 统计（debug 模式输出）
  const stats = {
    decoded: 0,
    painted: 0,
    droppedRender: 0,
    droppedAwaitingKey: 0,
    errors: 0
  }
  let statsTimer = null
  let lastDecodedSnapshot = 0

  if (debug) {
    statsTimer = setInterval(() => {
      const decodedFps = stats.decoded - lastDecodedSnapshot
      lastDecodedSnapshot = stats.decoded
      console.debug(
        '[scrcpy-webcodecs]',
        `fps=${decodedFps}`,
        `decoded=${stats.decoded}`,
        `painted=${stats.painted}`,
        `渲染丢帧=${stats.droppedRender}`,
        `等关键帧丢弃=${stats.droppedAwaitingKey}`,
        `解码队列=${decoder && decoder.state === 'configured' ? decoder.decodeQueueSize : 0}`,
        `错误=${stats.errors}`
      )
    }, 1000)
  }

  function fallback(reason) {
    if (destroyed || fellBack) return
    fellBack = true
    pendingPackets = []
    teardownDecoder()
    if (typeof onFallback === 'function') onFallback(reason)
  }

  function teardownDecoder() {
    if (decoder && decoder.state !== 'closed') {
      try {
        decoder.close()
      } catch {
        // 已关闭等状态异常可忽略
      }
    }
    decoder = null
  }

  /** VideoDecoder error 回调 / decode() 同步抛错的统一处理：丢弃后续 delta 直到下一个 key 并重新 configure */
  function handleDecodeFailure(err) {
    if (destroyed || fellBack) return
    stats.errors++
    consecutiveErrors++
    outputsSinceError = 0
    teardownDecoder()
    needsConfigure = true
    awaitingKeyFrame = true
    if (debug) console.debug('[scrcpy-webcodecs] 解码错误:', err?.message || err)
    if (consecutiveErrors >= maxConsecutiveErrors) {
      fallback(`decode-errors:${consecutiveErrors}`)
    }
  }

  function handleOutput(frame) {
    if (destroyed) {
      frame.close()
      return
    }
    stats.decoded++
    outputsSinceError++
    if (outputsSinceError >= STABLE_OUTPUTS_TO_RESET_ERRORS) consecutiveErrors = 0
    if (typeof onFrameDecoded === 'function') onFrameDecoded()

    if (!ctx) {
      frame.close()
      return
    }
    if (pendingFrame) {
      // 上一帧尚未绘制即被替换：立即释放，只保最新（低延迟 + 防泄漏）
      pendingFrame.close()
      stats.droppedRender++
    }
    pendingFrame = frame
    if (rafId === null) rafId = requestAnimationFrame(paintPendingFrame)
  }

  function paintPendingFrame() {
    rafId = null
    const frame = pendingFrame
    pendingFrame = null
    if (!frame) return
    try {
      if (!destroyed) {
        const width = frame.displayWidth || frame.codedWidth
        const height = frame.displayHeight || frame.codedHeight
        if (width > 0 && height > 0) {
          // 尺寸变化（如旋转）时同步画布内在尺寸，保持 CSS object-fit 行为与 <video> 一致
          if (canvas.width !== width) canvas.width = width
          if (canvas.height !== height) canvas.height = height
          ctx.drawImage(frame, 0, 0, width, height)
          stats.painted++
        }
      }
    } finally {
      frame.close()
    }
  }

  function configureDecoder(codec) {
    if (!decoder || decoder.state === 'closed') {
      decoder = new VideoDecoder({ output: handleOutput, error: handleDecodeFailure })
    } else if (decoder.state === 'configured') {
      decoder.reset()
    }
    // Annex-B 模式：不带 description；optimizeForLatency 提示解码器尽早出帧（实时流无 B 帧）
    decoder.configure({ codec, optimizeForLatency: true })
    needsConfigure = false
    awaitingKeyFrame = true
  }

  function beginSupportCheck(codec) {
    checkingSupport = true
    VideoDecoder.isConfigSupported({ codec, optimizeForLatency: true })
      .then((result) => Boolean(result && result.supported))
      .catch(() => false)
      .then((supported) => {
        codecSupportCache.set(codec, supported)
        checkingSupport = false
        if (destroyed || fellBack) return
        if (!supported) {
          fallback(`codec-unsupported:${codec}`)
          return
        }
        drainPendingPackets()
      })
  }

  function drainPendingPackets() {
    while (pendingPackets.length > 0 && !destroyed && !fellBack && !checkingSupport) {
      processPacket(pendingPackets.shift())
    }
  }

  function processPacket(bytes) {
    const packet = classifyAnnexBPacket(bytes)

    // 缓存/更新配置 NAL。copy 而非留 view，避免 SPS 视图长期钉住整条帧消息的 ArrayBuffer
    let configChanged = false
    if (packet.sps && !bytesEqual(packet.sps, spsNal)) {
      spsNal = packet.sps.slice()
      configChanged = true
    }
    if (packet.pps && !bytesEqual(packet.pps, ppsNal)) {
      ppsNal = packet.pps.slice()
      configChanged = true
    }
    if (configChanged) needsConfigure = true

    // 纯配置包（SPS/PPS）或无切片的杂项包：不喂 chunk（规范要求 chunk 恰含一个 primary coded picture）
    if (!packet.hasSlice || packet.frameNalUnits.length === 0) return

    // 解码积压保护：硬件停滞等异常时丢弃至下一个 IDR，防止延迟与内存无界增长
    if (
      decoder &&
      decoder.state === 'configured' &&
      decoder.decodeQueueSize > maxQueuedDecodes &&
      !needsConfigure
    ) {
      needsConfigure = true
      awaitingKeyFrame = true
      if (debug) console.debug('[scrcpy-webcodecs] 解码队列积压，丢弃至下一个关键帧')
    }

    if (needsConfigure || !decoder || decoder.state === 'closed') {
      // 新序列必须从 IDR 起播；等待期间丢弃 delta
      if (!packet.hasIdr) {
        stats.droppedAwaitingKey++
        return
      }
      if (!spsNal || !ppsNal) {
        // 后端契约保证 SPS/PPS 先于 IDR 到达，此分支仅防御异常序列
        stats.droppedAwaitingKey++
        return
      }
      const codec = codecStringFromSps(spsNal)
      if (!codec) {
        fallback('sps-parse-failed')
        return
      }
      const supported = codecSupportCache.get(codec)
      if (supported === undefined) {
        // 异步探测支持性，当前包（含 IDR）排队待重放
        if (pendingPackets.length >= MAX_PENDING_PACKETS) pendingPackets.shift()
        pendingPackets.push(bytes)
        beginSupportCheck(codec)
        return
      }
      if (!supported) {
        fallback(`codec-unsupported:${codec}`)
        return
      }
      try {
        configureDecoder(codec)
      } catch (err) {
        handleDecodeFailure(err)
        return
      }
    }

    if (awaitingKeyFrame && !packet.hasIdr) {
      stats.droppedAwaitingKey++
      return
    }

    // 组装 chunk 数据：
    // - key：拼接缓存 SPS/PPS + 帧 NAL（规范要求 annexb key chunk 自带全部参数集）
    // - delta：无配置 NAL 时直接透传原包（零拷贝快路径）
    let data
    if (packet.hasIdr) {
      data = buildAnnexBStream([spsNal, ppsNal, ...packet.frameNalUnits])
    } else if (!packet.sps && !packet.pps) {
      data = bytes
    } else {
      data = buildAnnexBStream(packet.frameNalUnits)
    }

    const chunk = new EncodedVideoChunk({
      type: packet.hasIdr ? 'key' : 'delta',
      timestamp: timestampUs,
      duration: frameDurationUs,
      data
    })
    timestampUs += frameDurationUs

    try {
      decoder.decode(chunk)
      if (packet.hasIdr) awaitingKeyFrame = false
    } catch (err) {
      handleDecodeFailure(err)
    }
  }

  /**
   * 喂入一条 WS 二进制消息（Annex-B 包）。
   * @param {Uint8Array} bytes
   */
  function push(bytes) {
    if (destroyed || fellBack || !bytes || bytes.length === 0) return
    if (checkingSupport) {
      if (pendingPackets.length >= MAX_PENDING_PACKETS) pendingPackets.shift()
      pendingPackets.push(bytes)
      return
    }
    processPacket(bytes)
  }

  /** 释放全部资源：解码器、待绘帧、rAF、缓冲与统计定时器。 */
  function destroy() {
    if (destroyed) return
    destroyed = true
    if (rafId !== null) {
      cancelAnimationFrame(rafId)
      rafId = null
    }
    if (pendingFrame) {
      pendingFrame.close()
      pendingFrame = null
    }
    pendingPackets = []
    if (statsTimer) {
      clearInterval(statsTimer)
      statsTimer = null
    }
    teardownDecoder()
  }

  return { push, destroy }
}

/**
 * WebCodecs 可用性静态检测（不含具体 codec 支持性，后者需拿到 SPS 后异步验证）。
 * @returns {boolean}
 */
export function isWebCodecsSupported() {
  return (
    typeof window !== 'undefined' &&
    'VideoDecoder' in window &&
    typeof window.VideoDecoder.isConfigSupported === 'function' &&
    typeof window.EncodedVideoChunk === 'function'
  )
}
