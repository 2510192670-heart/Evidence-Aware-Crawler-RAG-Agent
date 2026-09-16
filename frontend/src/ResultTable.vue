<script setup lang="ts">
import {ref,watch} from 'vue'
const props=defineProps<{taskId:string}>()
const page=ref(1),total=ref(0),items=ref<Record<string,unknown>[]>([]),fields=ref<string[]>([]),error=ref('')
const sources=ref<string[]>([])
let generation=0
async function load(){const run=++generation;error.value='';try{
 const r=await fetch(`/api/v1/tasks/${props.taskId}/result?page=${page.value}&page_size=25`)
 if(!r.ok)throw new Error('结果暂不可读取，请检查任务产物。')
 const result=await r.json();if(run!==generation)return;items.value=result.items;fields.value=result.fields;total.value=result.total;sources.value=result.source_urls||[]
}catch(e){if(run===generation)error.value=e instanceof Error?e.message:'读取失败'}}
watch(()=>props.taskId,()=>{page.value=1;void load()},{immediate:true})
async function move(delta:number){page.value+=delta;await load()}
</script>
<template><section class="panel"><h2>采集结果</h2>
 <p v-if="error" role="status" class="alert">{{error}} <button type="button" @click="load">重试</button></p>
 <template v-else><nav class="result-downloads" aria-label="结果下载"><a v-for="format in ['json','csv','xlsx']" :key="format" :href="`/api/v1/tasks/${taskId}/export/${format}`">下载 {{format.toUpperCase()}}</a></nav>
 <p class="hint">共 {{total}} 条；缺失值显示为“缺失”。CSV 对可能被表格软件解释为公式的文本加前导单引号。</p>
 <div class="table-scroll"><table><thead><tr><th v-for="field in fields" :key="field">{{field}}</th><th v-if="sources.length">来源</th></tr></thead><tbody><tr v-for="(row,index) in items" :key="index"><td v-for="field in fields" :key="field">{{row[field]??'缺失'}}</td><td v-if="sources.length"><a v-if="/^https?:\/\//.test(sources[index]||'')" :href="sources[index]" target="_blank" rel="noopener noreferrer">查看来源</a><span v-else>未记录</span></td></tr></tbody></table></div>
 <nav v-if="total>25" class="pagination" aria-label="结果分页"><button :disabled="page===1" @click="move(-1)">上一页</button><span>{{page}} / {{Math.ceil(total/25)}}</span><button :disabled="page*25>=total" @click="move(1)">下一页</button></nav></template>
</section></template>
