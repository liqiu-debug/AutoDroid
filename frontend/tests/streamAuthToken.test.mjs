import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'

import {
  buildIosMjpegImgUrl,
  buildIosMjpegWsUrl,
  buildScrcpyWsUrl,
  readStreamToken,
  withCurrentStreamToken,
  withStreamToken
} from '../src/utils/streamAuth.js'

// S4：R6' 鉴权收口后投屏 WS token 接线契约
// - /ws/scrcpy、/ws/ios-mjpeg、GET /api/stream/ios-mjpeg 的 URL 必须携带 ?token=
// - 两个投屏组件的 WS URL 构造必须走 streamAuth 收口（URL 含 token 的单测即锁于此）

const storageWith = (token) => ({ getItem: (key) => (key === 'token' ? token : null) })
const cloud = { protocol: 'https:', host: 'cloud.example', origin: 'https://cloud.example' }

test('stream WS URLs carry the login token as a query parameter', () => {
  const scrcpy = buildScrcpyWsUrl('USB:1', cloud, storageWith('tok-1'))
  assert.equal(scrcpy, 'wss://cloud.example/ws/scrcpy/USB%3A1?token=tok-1')
  assert.ok(scrcpy.includes('token=tok-1'))

  const ios = buildIosMjpegWsUrl('00008030-001', cloud, storageWith('tok-2'))
  assert.equal(ios, 'wss://cloud.example/ws/ios-mjpeg/00008030-001?token=tok-2')
  assert.ok(ios.includes('token=tok-2'))
})

test('iOS MJPEG REST fallback img URL carries the token too', () => {
  const img = buildIosMjpegImgUrl('udid', cloud, storageWith('tok-3'))
  assert.equal(img, 'https://cloud.example/api/stream/ios-mjpeg/udid?token=tok-3')
})

test('ws scheme follows the page protocol (http -> ws, https -> wss)', () => {
  const local = { protocol: 'http:', host: '127.0.0.1:5173' }
  assert.equal(buildScrcpyWsUrl('USB1', local, storageWith('t')), 'ws://127.0.0.1:5173/ws/scrcpy/USB1?token=t')
})

test('missing token leaves the URL untouched (web mode / pre-login)', () => {
  assert.equal(buildScrcpyWsUrl('USB1', cloud, storageWith(null)), 'wss://cloud.example/ws/scrcpy/USB1')
  assert.equal(buildIosMjpegWsUrl('USB1', cloud, storageWith('')), 'wss://cloud.example/ws/ios-mjpeg/USB1')
  assert.equal(readStreamToken(storageWith(null)), '')
})

test('withStreamToken overwrites a stale token and keeps existing query + hash', () => {
  assert.equal(
    withStreamToken('wss://h/ws/scrcpy/A?profile=hd&token=old#frag', 'new'),
    'wss://h/ws/scrcpy/A?profile=hd&token=new#frag'
  )
  assert.equal(withStreamToken('wss://h/p', 'x'), 'wss://h/p?token=x')
  assert.equal(withStreamToken('wss://h/p?token=x', ''), 'wss://h/p?token=x')
})

test('withCurrentStreamToken reads the token from localStorage', () => {
  assert.equal(withCurrentStreamToken('wss://h/p', storageWith('abc')), 'wss://h/p?token=abc')
})

test('both players build their WS URLs through streamAuth (token wired end to end)', () => {
  const scrcpySource = readFileSync(new URL('../src/components/ScrcpyPlayer.vue', import.meta.url), 'utf8')
  const iosSource = readFileSync(new URL('../src/components/IosMjpegPlayer.vue', import.meta.url), 'utf8')

  assert.match(scrcpySource, /return buildScrcpyWsUrl\(props\.serial\)/)
  assert.match(scrcpySource, /withCurrentStreamToken\(parsed\.toString\(\)\)/, 'custom stream URLs are token-wrapped as well')
  assert.doesNotMatch(scrcpySource, /`\/ws\/scrcpy\/\$\{encodeURIComponent\(props\.serial\)\}`/, 'raw scrcpy URL must not be built inline')

  assert.match(iosSource, /const wsUrl = buildIosMjpegWsUrl\(props\.serial\)/)
  assert.doesNotMatch(iosSource, /`\/ws\/ios-mjpeg\/\$\{encodeURIComponent\(props\.serial\)\}`/, 'raw ios-mjpeg URL must not be built inline')
})
