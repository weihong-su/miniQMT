<script setup lang="ts">
import { ref, onMounted } from 'vue'
import { loadConnection, saveConnection, checkSecurityWarning, isSecureContext, getCurrentAccount } from '../api/accounts'
import type { ConnectionSettings } from '../api/accounts'
import { useSystemStore } from '../stores/system'

const system = useSystemStore()

const emit = defineEmits<{ close: []; changed: [] }>()
const form = ref<ConnectionSettings>({ mode: 'auto', xtquantUrl: '', apiToken: '' })
const showToken = ref(false); const testing = ref(false); const testResult = ref('')
const securityWarning = ref<string | null>(null)

onMounted(() => { form.value = loadConnection(); securityWarning.value = checkSecurityWarning() })

function save() {
  if (form.value.mode === 'auto') form.value.mode = 'auto'
  saveConnection({ ...form.value }); securityWarning.value = checkSecurityWarning(); emit('changed')
}

async function testConnection() {
  testing.value = true; testResult.value = ''; save()
  const mode = form.value.mode
  try {
    let url: string
    if (mode === 'flask') {
      const acc = getCurrentAccount()
      const base = acc.flaskUrl || window.location.origin
      url = `${base}/api/status`
    } else {
      const base = form.value.xtquantUrl || window.location.origin
      url = `${base}/api/v1/health`
    }
    const headers: Record<string, string> = {}
    if (form.value.apiToken) headers['X-API-Token'] = form.value.apiToken
    const ctrl = new AbortController(); setTimeout(() => ctrl.abort(), 8000)
    const resp = await fetch(url, { headers, signal: ctrl.signal, mode: 'cors' })
    const ct = resp.headers.get('content-type') || ''
    if (!ct.includes('application/json')) {
      testResult.value = `✗ 服务器返回非 JSON (HTTP ${resp.status})，请检查地址是否正确`
      testing.value = false; return
    }
    const data = await resp.json()
    if (resp.ok && (data.success || data.status === 'success')) {
      if (mode === 'flask') {
        testResult.value = `✓ Flask 连接成功 — 账户 ${data.account?.id || 'OK'}`
      } else {
        const total = data.data?.total || 0
        const healthy = data.data?.healthy || 0
        testResult.value = `✓ 连接成功 — ${total} 个账号, ${healthy} 个在线`
      }
    } else {
      testResult.value = `✗ HTTP ${resp.status}: ${data.error || data.detail || ''}`
    }
  } catch (e: any) {
    if (e.name === 'AbortError') {
      testResult.value = '✗ 连接超时 (8s)，请检查服务器是否可达'
    } else if (e.message?.includes('Failed to fetch')) {
      testResult.value = '✗ 无法连接，请检查: 1) 服务是否启动 2) URL是否正确 3) 防火墙/CORS'
    } else {
      testResult.value = `✗ ${e.message}`
    }
  }
  testing.value = false
}
</script>

<template>
  <Teleport to="body">
    <div class="modal-overlay" @click.self="emit('close')">
      <div class="modal-content w-[540px] max-w-[96vw]">
        <div class="px-6 py-4 border-b border-slate-100">
          <h3 class="text-lg font-semibold text-slate-800">连接设置</h3>
          <p class="text-xs text-slate-400 mt-1">配置 QMT 后端服务器地址和 API Token</p>
        </div>
        <div class="p-6 space-y-5">
          <div v-if="securityWarning" class="bg-amber-50 border border-amber-200 rounded-lg p-3 text-xs text-amber-800 whitespace-pre-line">{{ securityWarning }}</div>

          <div :class="['flex items-center gap-2 text-xs px-3 py-2 rounded-lg', isSecureContext() ? 'bg-emerald-50 text-emerald-700' : 'bg-slate-50 text-slate-500']">
            <span :class="['w-2 h-2 rounded-full', isSecureContext() ? 'bg-emerald-500' : 'bg-slate-300']"></span>
            当前: <strong>{{ isSecureContext() ? 'HTTPS (安全)' : 'HTTP (本地)' }}</strong>
            <span v-if="isSecureContext()" class="text-emerald-600 text-[11px]">— 后端也必须 HTTPS</span>
          </div>

          <div>
            <label class="label-text mb-2">后端模式</label>
            <div class="grid grid-cols-1 sm:grid-cols-2 gap-2">
              <label :class="['flex items-start gap-2.5 p-3.5 rounded-lg border-2 cursor-pointer transition-all',
                form.mode === 'xtquant' ? 'border-blue-400 bg-blue-50' : 'border-slate-200 hover:border-slate-300']">
                <input type="radio" v-model="form.mode" value="xtquant" class="mt-0.5 accent-blue-600" />
                <div><div class="text-sm font-semibold text-slate-700">网关模式</div><div class="text-[11px] text-slate-400 mt-0.5">xtquant_manager 统一入口（推荐）</div></div>
              </label>
              <label :class="['flex items-start gap-2.5 p-3.5 rounded-lg border-2 cursor-pointer transition-all',
                form.mode === 'flask' ? 'border-blue-400 bg-blue-50' : 'border-slate-200 hover:border-slate-300']">
                <input type="radio" v-model="form.mode" value="flask" class="mt-0.5 accent-blue-600" />
                <div><div class="text-sm font-semibold text-slate-700">直连模式</div><div class="text-[11px] text-slate-400 mt-0.5">每账号独立 Flask 实例</div></div>
              </label>
            </div>
          </div>

          <div>
            <label class="label-text">{{ form.mode === 'flask' ? 'Flask 地址' : '网关地址' }}</label>
            <input v-if="form.mode === 'xtquant'" v-model="form.xtquantUrl" type="url" placeholder="http://127.0.0.1:8888" class="input-field font-mono" />
            <input v-else :value="system.currentAccount.flaskUrl || '(未设置)'" disabled type="url" class="input-field font-mono text-slate-400 bg-slate-50 cursor-not-allowed" />
            <p class="text-[10px] text-slate-400 mt-1">
              <template v-if="form.mode === 'xtquant'">所有账户共用此网关地址</template>
              <template v-else>直连模式下每个账户独立设置地址，请在账户下拉菜单中点击 ✎ 编辑</template>
            </p>
          </div>

          <div>
            <label class="label-text">API Token</label>
            <div class="relative">
              <input v-model="form.apiToken" :type="showToken ? 'text' : 'password'" placeholder="留空则不验证（仅限本机）" class="input-field font-mono pr-16" autocomplete="off" />
              <button @click="showToken = !showToken" class="absolute right-2 top-1/2 -translate-y-1/2 text-xs text-slate-400 hover:text-slate-600">{{ showToken ? '隐藏' : '显示' }}</button>
            </div>
          </div>

          <div class="bg-slate-50 rounded-lg p-4">
            <div class="flex items-center justify-between mb-2"><span class="text-sm font-medium text-slate-600">连通性测试</span>
              <button @click="testConnection" :disabled="testing" class="btn-outline btn-xs">{{ testing ? '测试中...' : '测试连接' }}</button>
            </div>
            <div v-if="testResult" :class="['text-xs font-mono px-3 py-2 rounded-lg', testResult.startsWith('✓') ? 'bg-emerald-50 text-emerald-700' : 'bg-red-50 text-red-700']">{{ testResult }}</div>
            <p v-else class="text-xs text-slate-400">点击测试按钮检查后端可达性</p>
          </div>
        </div>
        <div class="px-6 py-3 bg-slate-50/80 rounded-b-lg flex justify-end gap-2">
          <button @click="emit('close')" class="btn-ghost">关闭</button>
          <button @click="save(); emit('close')" class="btn-primary">保存</button>
        </div>
      </div>
    </div>
  </Teleport>
</template>
