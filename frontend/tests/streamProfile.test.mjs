import assert from 'node:assert/strict'
import test from 'node:test'

import {
  isNetworkDeviceSerial,
  shouldRestoreStoredStreamProfile
} from '../src/utils/streamProfile.js'
import { LIVE_PREVIEW_POLL_CONFIG } from '../src/composables/useLivePreviewPolling.js'

test('network serials start from the server low-latency profile', () => {
  assert.equal(isNetworkDeviceSerial('127.0.0.1:28101'), true)
  assert.equal(isNetworkDeviceSerial('192.168.1.8:5555'), true)
  assert.equal(shouldRestoreStoredStreamProfile('127.0.0.1:28101', 'hd', 'smooth'), false)
  assert.equal(shouldRestoreStoredStreamProfile('192.168.1.8:5555', 'standard', 'smooth'), false)
})

test('USB devices retain their stored profile behavior', () => {
  assert.equal(isNetworkDeviceSerial('R58N123456A'), false)
  assert.equal(shouldRestoreStoredStreamProfile('R58N123456A', 'hd', 'standard'), true)
  assert.equal(shouldRestoreStoredStreamProfile('R58N123456A', 'standard', 'standard'), false)
})

test('live hierarchy polling is low frequency except for a short interaction window', () => {
  assert.equal(LIVE_PREVIEW_POLL_CONFIG.androidIdleMs, 2500)
  assert.equal(LIVE_PREVIEW_POLL_CONFIG.androidActiveMs, 500)
  assert.equal(LIVE_PREVIEW_POLL_CONFIG.activeWindowMs, 1500)
  assert.equal(LIVE_PREVIEW_POLL_CONFIG.androidIdleMs > LIVE_PREVIEW_POLL_CONFIG.androidActiveMs, true)
})
