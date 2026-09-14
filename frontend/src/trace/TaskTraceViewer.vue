<script setup lang="ts">
import { shallowRef, ref, computed, watch, onBeforeUnmount } from 'vue'
import { useRoute } from 'vue-router'
import { read, taskPath, display, object, type Trace, type Json } from './api'
import TraceTimeline from './TraceTimeline.vue'
import ArtifactExplorer from './ArtifactExplorer.vue'
import JsonTree from './JsonTree.vue'
import './trace.css'
const route = useRoute()
const id = computed(() => String(route.params.taskId))
const trace = shallowRef<Trace | null>(null), error = ref(''), busy = ref(false)
const documents = shallowRef<Record<string, Json>>({}), documentErrors = ref<Record<string, string>>({})
const explorer = ref<InstanceType<typeof ArtifactExplorer>>()
let controller: AbortController | undefined
const names: Record<string, string> = { observing: 'Observing', analyzing: 'Analyzing', executing: 'Executing', verifying: 'Verifying' }
const files: Record<string, string> = { observing: 'evidence.json', analyzing: 'retrieval.json', executing: 'result.json', verifying: 'report.json' }
const diagnosis = computed(() => trace.value?.diagnostics.failure_retrieval)
const context = computed(() => object(diagnosis.value?.failure_context))
const gate = computed(() => object(object(documents.value['failure_retrieval.json']).knowledge_gate))
const repair = computed(() => object(documents.value['repair.json']))
const attempts = computed(() => Array.isArray(repair.value.attempts) ? repair.value.attempts : [])
function exists(name: string) { return trace.value?.artifacts.some(a => a.filename === name) }
function showArtifact(name: string) { explorer.value?.open(name); document.getElementById('artifacts')?.scrollIntoView({ behavior: 'smooth' }) }
async function load() {
  controller?.abort(); const current = controller = new AbortController()
  busy.value = true; error.value = ''; trace.value = null; documents.value = {}; documentErrors.value = {}
  const timeout = setTimeout(() => current.abort(), 20000)
  try {
    const value = await read<Trace>(taskPath(id.value) + '/trace', current.signal)
    if(controller !== current) return
    trace.value = value
    await Promise.all(['repair.json', 'failure_retrieval.json'].filter(exists).map(async name => {
      try { const doc = await read<Json>(taskPath(value.task_id) + '/artifacts/' + name, current.signal); if(controller === current) documents.value = { ...documents.value, [name]: doc } }
      catch(e) { if(controller === current) documentErrors.value[name] = current.signal.aborted ? '读取超时' : e instanceof Error ? e.message : '读取失败' }
    }))
  } catch(e) { if(controller === current) error.value = current.signal.aborted ? '读取超时，请重试' : e instanceof Error ? e.message : '无法连接服务' }
  finally { clearTimeout(timeout); if(controller === current) busy.value = false }
}
watch(id, load, { immediate: true })
onBeforeUnmount(() => { controller?.abort(); controller = undefined })
</script>
<template>
  <div class="trace-page">
    <header class="trace-header"><RouterLink :to="'/' + id">← Web Data Agent / Task Detail</RouterLink><span>READ ONLY <i></i></span></header>
    <main class="trace-main">
      <div class="trace-title"><div><p class="eyebrow">OBSERVE · UNDERSTAND · VERIFY</p><h1>Task Trace Viewer</h1><p class="trace-muted">每一步都有依据。查看任务如何执行，以及失败发生在哪里。</p></div><button :disabled="busy" @click="load">{{ busy ? '读取中…' : '刷新快照' }}</button></div>
      <div v-if="error" class="trace-error" role="alert">{{ error }} <button @click="load">重试</button></div>
      <p v-else-if="!trace" class="trace-empty" role="status">正在读取任务 Trace…</p>
      <template v-if="trace">
        <section class="trace-overview trace-card">
          <div class="overview-heading"><div><p class="eyebrow">TASK OVERVIEW</p><code>{{ trace.task_id }}</code></div><span class="trace-badge" :data-state="trace.status" data-testid="task-status">{{ display(trace.status) }}</span></div>
          <div class="trace-metrics"><div><small>Records</small><b>{{ display(trace.outcome.count) }}</b></div><div><small>Pages</small><b>{{ display(trace.outcome.pages) }}</b></div><div><small>Model calls</small><b data-testid="model-calls">{{ display(trace.model_calls) }}</b></div><div><small>Elapsed seconds</small><b>{{ display(trace.elapsed_seconds) }}</b></div></div>
          <p class="trace-metadata">Model {{ display(trace.model) }} · Source {{ display(trace.source) }} · Phase {{ trace.phase }} · Created {{ display(trace.created_at) }}</p>
          <div v-if="trace.outcome.error" class="trace-error" role="alert"><b>{{ display(trace.outcome.error) }}</b><p>{{ display(trace.outcome.error_category) }} · {{ display(trace.outcome.error_type) }}</p></div>
        </section>
        <div class="trace-layout"><TraceTimeline :steps="trace.steps" :available="trace.timeline_available" />
          <div class="trace-details">
            <section class="trace-card"><p class="eyebrow">EXECUTION EVIDENCE</p><h2>Evidence Explorer</h2>
              <article v-for="step in trace.steps" :id="'stage-' + step.key" :key="step.key" class="stage-evidence">
                <div class="section-heading"><h3>{{ names[step.key] ?? step.key }}</h3><span class="trace-badge" :data-state="step.state">{{ step.state }}</span></div>
                <p class="trace-muted">记录时间 {{ display(step.at) }} · API duration {{ display(step.duration_s) }}{{ step.duration_s === null ? '' : ' s' }}</p>
                <JsonTree v-if="step.evidence" :value="step.evidence" label="evidence" /><p v-else class="trace-muted">该阶段暂无证据。</p>
                <button v-if="exists(files[step.key])" @click="showArtifact(files[step.key])">查看 {{ files[step.key] }}</button>
              </article>
            </section>
            <section class="trace-card"><p class="eyebrow">CASE RETRIEVAL</p><h2>Retrieval Insight</h2>
              <p class="trace-muted">规划阶段的检索证据；不代表检索改变了执行结果。</p>
              <JsonTree :value="trace.steps.find(s => s.key === 'analyzing')?.evidence?.retrieval" label="retrieval" />
              <button v-if="exists('retrieval.json')" @click="showArtifact('retrieval.json')">查看完整 retrieval.json</button>
            </section>
            <section id="diagnosis" class="trace-card"><p class="eyebrow">ADVISORY EVIDENCE</p><h2>Failure Diagnosis</h2>
              <template v-if="diagnosis">
                <dl class="trace-facts"><dt>Error Code</dt><dd>{{ display(context.error_code) }}</dd><dt>Category</dt><dd>{{ display(context.category) }}</dd><dt>Context Phase</dt><dd>{{ display(context.phase) }}</dd><dt>Failure Phase</dt><dd>{{ display(diagnosis.failure_phase) }}</dd></dl>
                <h3>Knowledge Retrieval</h3><dl class="trace-facts"><dt>Case IDs</dt><dd>{{ display(diagnosis.case_ids) }}</dd><dt>Knowledge gate applied</dt><dd>{{ display(diagnosis.knowledge_gate_applied) }}</dd><dt>Matched Cases</dt><dd>{{ display(gate.matched) }}</dd><dt>Filtered Cases</dt><dd>{{ display(gate.filtered) }}</dd></dl>
                <p v-if="documentErrors['failure_retrieval.json']" class="trace-error">{{ documentErrors['failure_retrieval.json'] }}</p>
                <div class="trace-advisory"><b>Safety Boundary · 架构约束</b><p>✓ Advisory only　 ✓ No repair influence　 ✓ No second LLM call</p><p>诊断不参与重新规划、执行或修复决策。</p></div>
              </template><p v-else class="trace-empty">未记录诊断产物。缺少诊断不代表任务成功或无需诊断。</p>
            </section>
            <section id="repair" class="trace-card"><p class="eyebrow">DETERMINISTIC POLICY</p><h2>Repair Audit</h2>
              <dl class="trace-facts"><dt>Attempts</dt><dd>{{ display(object(trace.repair.document).attempts) }}</dd><dt>Proposals</dt><dd>{{ display(object(trace.repair.document).proposals) }}</dd><dt>Applied（报告）</dt><dd>{{ display(trace.repair.applied) }}</dd><dt>Applied（审计）</dt><dd>{{ display(repair.applied) }}</dd><dt>Outcome</dt><dd>{{ display(trace.repair.outcome) }}</dd><dt>Proposal Hash</dt><dd>{{ display(repair.candidate_plan_hash) }}</dd><dt>Authority</dt><dd><code>repair.py</code></dd></dl>
              <h3>Policy</h3><p v-if="!attempts.length" class="trace-muted">未记录策略判定。</p>
              <dl v-for="(attempt, index) in attempts" :key="index" class="trace-facts"><dt>Reason code</dt><dd>{{ display(object(attempt).reason_code) }}</dd><dt>Rejection code</dt><dd>{{ display(object(attempt).rejection_code) }}</dd><dt>Validation</dt><dd>{{ display(object(attempt).validation_result) }}</dd></dl>
              <p v-if="documentErrors['repair.json']" class="trace-error">{{ documentErrors['repair.json'] }}</p>
              <div class="trace-advisory"><b>LLM proposes → Deterministic validation → Bounded repair</b><p>LLM 仅提出初始计划；本阶段修复 proposal 由确定性代码生成，repair.py 控制是否应用。</p></div>
            </section>
            <ArtifactExplorer ref="explorer" :key="id" :task-id="id" :artifacts="trace.artifacts" />
          </div>
        </div>
        <footer class="trace-footer">Trace schema {{ trace.trace_schema_version }} · 只读快照 · 未记录的值保持未知</footer>
      </template>
    </main>
  </div>
</template>
