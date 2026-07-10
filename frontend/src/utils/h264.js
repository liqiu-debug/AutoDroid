/**
 * h264.js - H.264 Annex-B 码流解析纯函数
 *
 * 服务于 ScrcpyPlayer 的 WebCodecs 解码路径：
 * 后端 `WS /ws/scrcpy/{serial}` 每条二进制消息是一个裸 Annex-B H.264 包
 * （带 00 00 01 / 00 00 00 01 start code，SPS/PPS 为独立 config 包）。
 * 这里提供 NAL 拆分、类型判定、codec 串推导与 Annex-B 重组的无副作用工具。
 */

/** H.264 NAL 单元类型（nal_unit_type，取头字节低 5 位） */
export const NAL_TYPE = {
  SLICE_NON_IDR: 1,
  PARTITION_A: 2,
  PARTITION_B: 3,
  PARTITION_C: 4,
  SLICE_IDR: 5,
  SEI: 6,
  SPS: 7,
  PPS: 8,
  AUD: 9
}

/**
 * 读取 NAL 单元类型。
 * @param {Uint8Array} nal NAL 单元（不含 start code，首字节为 NAL header）
 * @returns {number} nal_unit_type；无效输入返回 -1
 */
export function getNalType(nal) {
  if (!nal || nal.length === 0) return -1
  return nal[0] & 0x1f
}

/**
 * 将一段 Annex-B 字节流按 start code（00 00 01 / 00 00 00 01）拆为 NAL 单元。
 * 返回的是原缓冲区上的 subarray 视图（零拷贝），首字节为 NAL header。
 * 4 字节 start code 的前导 0 会从上一个 NAL 的尾部剔除。
 * @param {Uint8Array} bytes
 * @returns {Uint8Array[]}
 */
export function splitAnnexBNalUnits(bytes) {
  const units = []
  if (!bytes || bytes.length < 4) return units

  const len = bytes.length
  let nalStart = -1

  for (let i = 0; i + 2 < len; i++) {
    if (bytes[i] !== 0x00 || bytes[i + 1] !== 0x00 || bytes[i + 2] !== 0x01) continue
    if (nalStart >= 0) {
      let nalEnd = i
      // 兼容 4 字节 start code（00 00 00 01）：多出的 0 属于 start code 而非上一个 NAL
      if (nalEnd > nalStart && bytes[nalEnd - 1] === 0x00) nalEnd--
      if (nalEnd > nalStart) units.push(bytes.subarray(nalStart, nalEnd))
    }
    nalStart = i + 3
    i += 2
  }

  if (nalStart >= 0 && nalStart < len) {
    units.push(bytes.subarray(nalStart, len))
  }
  return units
}

/**
 * 由 SPS NAL 推导 WebCodecs codec 串 `avc1.PPCCLL`。
 * profile_idc / constraint 标志 / level_idc 位于 SPS 头字节后的前三个字节，
 * 此区间不受 emulation prevention 影响（profile_idc 恒非 0），可直接读取。
 * @param {Uint8Array} spsNal SPS NAL 单元（首字节为 0x67 一类的 NAL header）
 * @returns {string|null} 如 'avc1.42c029'；输入无效返回 null
 */
export function codecStringFromSps(spsNal) {
  if (!spsNal || spsNal.length < 4 || getNalType(spsNal) !== NAL_TYPE.SPS) return null
  const hex = (byte) => byte.toString(16).padStart(2, '0')
  return `avc1.${hex(spsNal[1])}${hex(spsNal[2])}${hex(spsNal[3])}`
}

/**
 * 对单条 WS 消息（一个 Annex-B 包）做 NAL 级分类：
 * - 抽出 SPS/PPS（配置信息，需缓存而非直接作为 chunk 喂解码器）
 * - 保留帧相关 NAL（切片 / SEI / AUD 等）用于组装 EncodedVideoChunk
 * @param {Uint8Array} bytes
 * @returns {{sps: Uint8Array|null, pps: Uint8Array|null, hasIdr: boolean, hasSlice: boolean, frameNalUnits: Uint8Array[]}}
 */
export function classifyAnnexBPacket(bytes) {
  const nalUnits = splitAnnexBNalUnits(bytes)
  let sps = null
  let pps = null
  let hasIdr = false
  let hasSlice = false
  const frameNalUnits = []

  for (const nal of nalUnits) {
    const type = getNalType(nal)
    if (type === NAL_TYPE.SPS) {
      sps = nal
      continue
    }
    if (type === NAL_TYPE.PPS) {
      pps = nal
      continue
    }
    if (type === NAL_TYPE.SLICE_IDR) {
      hasIdr = true
      hasSlice = true
    } else if (type >= NAL_TYPE.SLICE_NON_IDR && type <= NAL_TYPE.PARTITION_C) {
      hasSlice = true
    }
    frameNalUnits.push(nal)
  }

  return { sps, pps, hasIdr, hasSlice, frameNalUnits }
}

/**
 * 用 4 字节 start code 将 NAL 单元序列重组为一段 Annex-B 字节流。
 * @param {Uint8Array[]} nalUnits
 * @returns {Uint8Array}
 */
export function buildAnnexBStream(nalUnits) {
  let total = 0
  for (const nal of nalUnits) total += 4 + nal.length

  const out = new Uint8Array(total)
  let offset = 0
  for (const nal of nalUnits) {
    // 00 00 00 01（out 初始为全 0，只需写入结尾的 01）
    out[offset + 3] = 0x01
    out.set(nal, offset + 4)
    offset += 4 + nal.length
  }
  return out
}

/**
 * 字节级相等比较（用于探测 SPS/PPS 是否发生变化，如旋转导致的重编码）。
 * @param {Uint8Array|null} a
 * @param {Uint8Array|null} b
 * @returns {boolean}
 */
export function bytesEqual(a, b) {
  if (a === b) return true
  if (!a || !b || a.length !== b.length) return false
  for (let i = 0; i < a.length; i++) {
    if (a[i] !== b[i]) return false
  }
  return true
}
