<script setup lang="ts">
import { shallowRef, ref, watch, onBeforeUnmount } from 'vue'
import { read, taskPath, display, type Trace, type Json } from './api'
import JsonTree from './JsonTree.vue'
const props = defineProps<{ taskId: string; artifacts: Trace['artifacts'] }>()
const filename = ref(''), content = shallowRef<Json>(null), error = ref(''), busy = ref(false)
let controller: AbortController | undefined
async function open(name: string) {
  controller?.abort(); const current = controller = new AbortController()
  filename.value = name; error.value = ''; content.value = null; busy.value = true
  const timeout = setTimeout(() => current.abort(), 20000)
  try { const value = await read<Json>(taskPath(props.taskId) + '/artifacts/' + encodeURIComponent(name), current.signal); if(controller === current) content.value = value }
  catch(e) { if(controller === current) error.value = current.signal.aborted ? '读取超时，请重试' : e instanceof Error ? e.message : '读取失败' }
  finally { clearTimeout(timeout); if(controller === current) busy.value = false }
}
watch(() => props.taskId, () => { controller?.abort(); controller = undefined; filename.value = ''; content.value = null; error.value = ''; busy.value = false })
onBeforeUnmount(() => { controller?.abort(); controller = undefined })
defineExpose({ open })
</script>
<template>
  <section id="artifacts" class="trace-card">
    <p class="eyebrow">PERSISTED EVIDENCE</p><h2>Artifact Explorer</h2>
    <p class="trace-muted">只读查看已注册 JSON；文件内容按原值展示。</p>
    <p v-if="!artifacts.length" class="trace-empty">暂无已注册产物。</p>
    <div class="artifact-list"><div v-for="artifact in artifacts" :key="artifact.filename" class="artifact-row">
      <div><b>{{ artifact.filename }}</b><small>{{ display(artifact.size_bytes) }} bytes</small><code class="artifact-hash">SHA256 {{ display(artifact.sha256) }}</code></div>
      <button v-if="artifact.filename.endsWith('.json')" :aria-label="'打开 ' + artifact.filename" @click="open(artifact.filename)">查看 JSON ↗</button>
      <span v-else class="trace-muted">仅支持原控制台下载</span>
    </div></div>
    <div v-if="filename" class="artifact-preview" data-testid="artifact-content" aria-live="polite">
      <h3>{{ filename }}</h3><p v-if="busy">正在读取…</p>
      <div v-else-if="error" role="alert">{{ error }} <button @click="open(filename)">重试读取</button></div>
      <JsonTree v-else :key="filename" :value="content" mode="raw" />
    </div>
  </section>
</template>
