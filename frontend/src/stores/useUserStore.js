import { defineStore } from 'pinia'
import { ref, computed } from 'vue'
import api from '../api'

export const useUserStore = defineStore('user', () => {
    const token = ref(localStorage.getItem('token') || '')
    const userInfo = ref(null)
    const defaultFeatureFlags = {
        model_inspection: false,
        inspection_identity_v2: false,
        inspection_similarity_convergence: false,
        inspection_exploration_family_convergence: false,
        content_addressed_assets: false,
        tiered_asset_retention: false,
    }
    const featureFlags = ref({ ...defaultFeatureFlags })

    const isLoggedIn = computed(() => !!token.value)
    const isAdmin = computed(() => userInfo.value?.role === 'admin')

    // 鉴权失败（401 / 本地会话门闩 ERR_SESSION_EXPIRED）才被动登出；
    // 瞬时网络失败/超时等非鉴权错误不得销毁有效会话（劣网抖动保 token）。
    const isAuthFailure = (error) => {
        if (!error) return false
        if (error.authFailure === true) return true
        if (error.response?.status === 401) return true
        return error.sessionExpired === true || error.code === 'ERR_SESSION_EXPIRED'
    }

    async function login(username, password, options = {}) {
        try {
            const params = new URLSearchParams()
            params.append('username', username)
            params.append('password', password)

            // options.server：客户端模式下的上游地址（X-AutoDroid-Server），Web 模式不传。
            const response = await api.login(params, { server: options?.server })
            token.value = response.data.access_token
            localStorage.setItem('token', token.value)
            try {
                await fetchUserInfo()
            } catch (error) {
                // 登录已换到有效 token：用户信息拉取的非鉴权失败（网络抖动/超时）
                // 不回滚登录，守卫/页面会惰性重试；鉴权失败则 fetchUserInfo 已
                // logout 并标记 authFailure，照常整体失败。
                if (error?.authFailure) throw error
            }
            return true
        } catch (error) {
            console.error('Login failed:', error)
            throw error
        }
    }

    // options.silent401：路由守卫取 userInfo 的请求标记（拦截器对该请求不弹
    // 「会话过期」框），由守卫静默 next('/login')；弹框只服务会话中途的后台 401。
    async function fetchUserInfo(options = {}) {
        try {
            const response = await api.getUserInfo({ silent401: options?.silent401 === true })
            userInfo.value = response.data
            await fetchFeatureFlags()
        } catch (error) {
            console.error('Fetch user info failed:', error)
            const authFailure = isAuthFailure(error)
            if (error && typeof error === 'object') {
                // rethrow 时标记错误类别：守卫按 authFailure 分流（鉴权失败收口
                // /login，非鉴权错误放行进入，页面级请求自报错）。
                error.authFailure = authFailure
            }
            // 仅鉴权失败被动登出并向上抛（守卫 catch 生效）；非鉴权错误保会话。
            if (authFailure) logout()
            throw error
        }
    }

    async function fetchFeatureFlags() {
        try {
            const response = await api.getFeatureFlags()
            featureFlags.value = { ...defaultFeatureFlags, ...(response.data || {}) }
        } catch {
            featureFlags.value = { ...defaultFeatureFlags }
        }
    }

    // manual=true = 用户主动「退出登录」：置 loggedOut 标记（跨重启也不再自动登录，
    // 直到重新手动登录并勾选自动登录）。401/取用户信息失败等被动登出保持默认
    // manual=false，不置标记。标记失败静默，绝不影响登出本身。
    function logout(manual = false) {
        token.value = ''
        userInfo.value = null
        featureFlags.value = { ...defaultFeatureFlags }
        localStorage.removeItem('token')
        if (manual === true) {
            try {
                // 同步调用即可（桥缺失时 api 层已优雅降级 no-op），失败静默。
                // 契约：markLoggedLogout(true) 传参标记用户主动登出。
                const mark = api.markLoggedLogout || api.markLoggedOut
                if (typeof mark === 'function') {
                    const pending = mark.call(api, true)
                    if (pending && typeof pending.catch === 'function') pending.catch(() => {})
                }
            } catch { /* 标记失败不影响登出 */ }
        }
        // Router redirect handled in component or router guard
    }

    return {
        token,
        userInfo,
        featureFlags,
        isLoggedIn,
        isAdmin,
        login,
        fetchUserInfo,
        fetchFeatureFlags,
        logout
    }
})
