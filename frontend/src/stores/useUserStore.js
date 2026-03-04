import { defineStore } from 'pinia'
import { ref, computed } from 'vue'
import api from '../api'

export const useUserStore = defineStore('user', () => {
    const token = ref(localStorage.getItem('token') || '')
    const userInfo = ref(null)

    const isLoggedIn = computed(() => !!token.value)

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
        } catch (error) {
            console.error('Fetch user info failed:', error)
            logout()
        }
    }

    function logout() {
        token.value = ''
        userInfo.value = null
        localStorage.removeItem('token')
        // Router redirect handled in component or router guard
    }

    return {
        token,
        userInfo,
        isLoggedIn,
        login,
        fetchUserInfo,
        logout
    }
})
