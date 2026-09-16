<script setup lang="ts">
import {onMounted, ref} from 'vue'
const emit=defineEmits<{select:[id:string]}>()
const profiles=ref<{id:string,name:string,model:string}[]>([]), selected=ref('')
const name=ref('我的模型'), baseUrl=ref('https://api.deepseek.com'), model=ref(''), key=ref('')
const busy=ref(false), error=ref(''),responseMode=ref('json_object'),thinking=ref('')
const probeResult=ref('')
async function probe(){busy.value=true;error.value='';probeResult.value='';try{const r=await fetch(`/api/v1/models/${selected.value}/check`,{method:'POST'});const value=await r.json();if(!r.ok)throw new Error(`模型测试失败：${value.code}`);probeResult.value=`连接及结构化响应通过，共 ${value.model_calls} 次请求。`}catch(e){error.value=e instanceof Error?e.message:'测试失败'}finally{busy.value=false}}
async function remove(){busy.value=true;error.value='';try{const r=await fetch(`/api/v1/models/${selected.value}`,{method:'DELETE'});if(!r.ok)throw new Error('移除失败');selected.value='';emit('select','');probeResult.value='';await refresh()}catch(e){error.value=e instanceof Error?e.message:'移除失败'}finally{busy.value=false}}
async function refresh(){const r=await fetch('/api/v1/models');if(!r.ok)throw new Error('无法读取模型配置');profiles.value=(await r.json()).items}
async function save(){busy.value=true;error.value='';try{
 const r=await fetch('/api/v1/models',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name:name.value,base_url:baseUrl.value,model:model.value,api_key:key.value,response_mode:responseMode.value,thinking:thinking.value||null})})
 if(!r.ok)throw new Error('配置无效：请检查 HTTPS API 地址、模型名称和密钥。')
 const profile=await r.json();key.value='';await refresh();selected.value=profile.id;emit('select',selected.value)
}catch(e){error.value=e instanceof Error?e.message:'保存失败'}finally{busy.value=false}}
onMounted(()=>refresh().catch(()=>{error.value='模型列表暂时无法加载'}))
</script>
<template>
 <label>使用模型<select v-model="selected" @change="emit('select',selected)"><option value="">服务端默认模型</option><option v-for="p in profiles" :key="p.id" :value="p.id">{{p.name}} · {{p.model}}</option></select></label>
 <details><summary>配置自己的模型</summary>
 <p class="hint">支持兼容 OpenAI Chat Completions 的 HTTPS API。配置仅保留在本次服务运行内，重启后需重新填写。</p>
 <label>配置名称<input v-model="name" maxlength="80"></label>
 <label>API 地址<input v-model="baseUrl" type="url" maxlength="512"></label>
 <label>模型名称<input v-model="model" maxlength="160" placeholder="填写供应商提供的模型 ID"></label>
 <label>API 密钥<input v-model="key" type="password" autocomplete="new-password" maxlength="4096"></label>
 <label>结构化输出<select v-model="responseMode"><option value="json_object">JSON Object</option><option value="json_schema">JSON Schema</option><option value="none">不附加响应格式参数</option></select></label>
 <label>思考模式<select v-model="thinking"><option value="">使用供应商默认</option><option value="disabled">关闭（供应商需支持）</option><option value="enabled">开启（供应商需支持）</option></select></label>
 <button type="button" @click="save" :disabled="busy||!key.trim()||!model.trim()||!baseUrl.trim()">{{busy?'保存中…':'保存并选择模型'}}</button>
 <button v-if="selected" type="button" @click="probe" :disabled="busy">测试连接（调用模型）</button>
 <button v-if="selected" type="button" @click="remove" :disabled="busy">移除当前配置</button>
 <p v-if="probeResult" role="status">{{probeResult}}</p>
 <p v-if="error" role="status" class="alert">{{error}}</p>
 <small>保存不调用模型。密钥不写入任务、数据库、日志或结果文件。</small>
 </details>
</template>
