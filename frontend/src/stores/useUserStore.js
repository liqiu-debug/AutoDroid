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

    async function login(username, password, options = {}) {
        try {
            const params = new URLSearchParams()
            params.append('username', username)
            params.append('password', password)

            // options.server：客户端模式下的上游地址（X-AutoDroid-Server），Web 模式不传。
            const response = await api.login(params, { server: options?.server })
            token.value = response.data.access_token
            localStorage.setItem('token', token.value)
            await fetchUserInfo()
            return true
        } catch (error) {
            console.error('Login failed:', error)
            throw error
        }
    }

    async function fetchUserInfo() {
        try {
            const response = await api.getUserInfo()
            userInfo.value = response.data
            await fetchFeatureFlags()
        } catch (error) {
            console.error('Fetch user info failed:', error)
            logout()
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
