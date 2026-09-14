<script setup lang="ts">
import { computed, ref, type PropType } from 'vue'
import type { Json } from './api'
import { jsonLeaf } from './display'
const props = defineProps({
  // Disable Vue Boolean prop casting: JSON empty strings must remain strings.
  value: { type: null as unknown as PropType<Json | undefined>, required: true },
  label: { type: String, default: 'JSON' },
  depth: { type: Number, default: 0 },
  mode: { type: String as PropType<'summary' | 'raw'>, default: 'summary' },
})
const expanded = ref(props.depth < 2)
const limit = ref(50)
const entries = computed(() => props.value !== null && typeof props.value === 'object' ? Object.entries(props.value) : null)
</script>
<template>
  <details v-if="entries" class="json-node" :open="expanded" @toggle="expanded = ($event.target as HTMLDetailsElement).open">
    <summary><span>{{ label }}</span> <small>{{ Array.isArray(value) ? 'Array' : 'Object' }} · {{ entries.length }}</small></summary>
    <div v-if="expanded" class="json-children">
      <JsonTree v-for="[key, item] in entries.slice(0, limit)" :key="key" :value="item" :label="key" :depth="depth + 1" :mode="mode" />
      <button v-if="entries.length > limit" @click="limit += 50">显示更多（剩余 {{ entries.length - limit }}）</button>
      <span v-if="!entries.length" class="trace-muted">{{ Array.isArray(value) ? (mode === 'raw' ? '[]' : '无记录') : '{}' }}</span>
    </div>
  </details>
  <div v-else class="json-leaf"><span>{{ label }}:</span> <code>{{ jsonLeaf(value, mode) }}</code></div>
</template>
