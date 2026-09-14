<script setup lang="ts">
import { display, type Step } from './api'
defineProps<{ steps: Step[]; available: boolean }>()
const names: Record<string, string> = { observing: 'Observing', analyzing: 'Analyzing', executing: 'Executing', verifying: 'Verifying' }
const states = { reached: '已到达', failed: '失败', pending: '待到达', absent: '无阶段证据' }
function scroll(key: string) { document.getElementById('stage-' + key)?.scrollIntoView({ behavior: 'smooth' }) }
</script>
<template>
  <aside class="trace-timeline trace-card">
    <p class="eyebrow">AGENT LIFECYCLE</p><h2>Agent Timeline</h2>
    <p v-if="!available" class="trace-muted">暂无时间线事件；不重建执行过程。</p>
    <ol>
      <li v-for="(step, index) in steps" :key="step.key" class="trace-step" :data-state="step.state">
        <span class="step-dot">{{ step.state === 'failed' ? '!' : index + 1 }}</span>
        <div><a :href="'#stage-' + step.key" @click.prevent="scroll(step.key)">{{ names[step.key] ?? step.key }}</a>
          <p>{{ states[step.state] }}</p><small>{{ display(step.duration_s) }}{{ step.duration_s === null ? '' : ' s' }}</small>
        </div>
      </li>
    </ol>
    <div class="trace-advisory"><b>Repair · Diagnostics</b><p>下方独立展示审计与诊断证据，不代表额外执行阶段。</p></div>
  </aside>
</template>
