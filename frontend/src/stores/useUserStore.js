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

    async function login(username, password) {
        try {
            const params = new URLSearchParams()
            params.append('username', username)
            params.append('password', password)

            const response = await api.login(params)
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

    function logout() {
        token.value = ''
        userInfo.value = null
        featureFlags.value = { ...defaultFeatureFlags }
        localStorage.removeItem('token')
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
