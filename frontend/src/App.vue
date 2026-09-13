<script setup lang="ts">
import {ref, computed, onMounted, onUnmounted, watch} from 'vue'
import {useRoute,useRouter} from 'vue-router'
type Task={id:string,status:string,created_at:string,spec:{url:string,rag_enabled?:boolean},summary:Record<string,any>|null}
type Artifact={id:string,filename:string,size_bytes:number}
const route=useRoute(), router=useRouter()
const tasks=ref<Task[]>([]), selected=ref<Task|null>(null), artifacts=ref<Artifact[]>([]), events=ref<{seq:number,status:string}[]>([])
const page=ref(1),total=ref(0),error=ref(''), actionError=ref(''),loading=ref(true),pending=ref(false),configured=ref(false)
const url=ref('http://127.0.0.1:8000/'),fields=ref('id,name,price_fen'),maxPages=ref(3),clickText=ref('下一页'),rag=ref(true)
const curlText=ref(''), curlPreview=ref<{executable:boolean,url:string|null,query:Record<string,string>,method?:string,request_body?:Record<string,unknown>|null,reasons:string[],notes?:string[]}|null>(null), previewBusy=ref(false)
watch(curlText,()=>{curlPreview.value=null})
async function previewCurl(){previewBusy.value=true;actionError.value='';curlPreview.value=null;try{curlPreview.value=await request('/import/curl',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({text:curlText.value})})}catch(e){actionError.value=explain(e)}finally{previewBusy.value=false}}
const terminal=['succeeded','failed','cancelled','interrupted']
const labels:Record<string,string>={created:'已创建',observing:'观察中',analyzing:'分析中',executing:'采集中',verifying:'验证中',succeeded:'已完成',failed:'失败',cancelled:'已取消',cancelling:'取消中',interrupted:'已中断'}
const stages=['created','observing','analyzing','executing','verifying','succeeded'],stageNames=['创建','观察','分析','执行','验证','完成']
const active=computed(()=>tasks.value.some(t=>!terminal.includes(t.status)) || (selected.value&&!terminal.includes(selected.value.status)))
const summary=computed(()=>selected.value?.summary)
const message:Record<string,string>={unsupported_curl:'无法解析：请使用 Copy as cURL (bash)，目前只接受 URL、-X、-H 和 --compressed。',task_already_running:'已有任务正在运行，请等待或取消后重试。',model_not_configured:'服务端未配置模型密钥，请检查启动环境。',invalid_request:'输入不符合要求，请检查地址、字段和页数。',artifact_changed:'文件已变化，下载已停止。',task_not_found:'任务不存在。',cross_origin_rejected:'请求来源不匹配，请使用服务端控制台地址。'}
async function request(path:string,options:RequestInit={}){const r=await fetch('/api/v1'+path,{signal:AbortSignal.timeout(20000),...options});if(!r.ok){const b=await r.json().catch(()=>({}));throw new Error(message[b.code]||`请求失败（${r.status}）`)}return r.json()}
function explain(e:unknown){return e instanceof TypeError?'无法连接服务，请确认后端已启动。':e instanceof Error?e.message:'操作失败，请重试。'}
let timer:ReturnType<typeof setTimeout>|undefined, alive=true,refreshing=false, generation=0
async function refresh(){if(refreshing)return;refreshing=true;const g=generation,id=String(route.params.taskId||'');try{
 const [health,list]=await Promise.all([request('/health'),request(`/tasks?page=${page.value}&page_size=5`)]);
 if(!alive||g!==generation)return;configured.value=health.model.configured;tasks.value=list.items;total.value=list.total
 if(id){const [task,ev,files]=await Promise.all([request('/tasks/'+id),request('/tasks/'+id+'/events'),request('/tasks/'+id+'/artifacts')]);if(!alive||g!==generation)return;selected.value=task;events.value=ev.items;artifacts.value=files.items}
 else if(list.items.length) {await router.replace('/'+list.items[0].id)}
 error.value=''
 }catch(e){if(alive&&g===generation)error.value=explain(e)}finally{loading.value=false;refreshing=false}}
async function poll(){await refresh();if(alive)timer=setTimeout(poll,1500)}
watch(()=>route.params.taskId,()=>{generation++;selected.value=null;artifacts.value=[];events.value=[];actionError.value='';void refresh()})
async function changePage(delta:number){page.value+=delta;generation++;await refresh()}
async function create(){if(pending.value)return;pending.value=true;actionError.value='';try{const task=await request('/tasks',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({url:url.value.trim(),fields:fields.value.split(',').map(x=>x.trim()).filter(Boolean),max_pages:maxPages.value,click_text:clickText.value,rag_enabled:rag.value,imported_url:curlPreview.value?.executable?curlPreview.value.url:null,imported_method:curlPreview.value?.executable?(curlPreview.value.method||'GET'):'GET',imported_request_body:curlPreview.value?.executable?(curlPreview.value.request_body??null):null})});page.value=1;await router.push('/'+task.id);await refresh()}catch(e){actionError.value=explain(e)}finally{pending.value=false}}
async function cancel(){if(!selected.value)return;pending.value=true;actionError.value='';try{await request('/tasks/'+selected.value.id+'/cancel',{method:'POST'});await refresh()}catch(e){actionError.value=explain(e)}finally{pending.value=false}}
function date(s:string){return new Date(s).toLocaleString('zh-CN',{hour12:false})}
function reached(s:string){return events.value.some(e=>e.status===s)}
onMounted(poll);onUnmounted(()=>{alive=false;clearTimeout(timer)})
</script>

<template>
 <header><strong>Web Data Agent</strong><a href="/docs" target="_blank" rel="noopener">API 文档</a></header>
 <main><h1>任务控制台</h1><p class="intro">从页面观察到结构化数据。</p>
 <div v-if="error" class="alert" role="alert">{{error}} <button @click="refresh">重试</button></div>
 <div v-if="actionError" class="alert" role="alert">{{actionError}}</div>
 <div class="layout">
 <section class="panel form"><h2>创建任务</h2><form @submit.prevent="create">
 <details class="curl-import"><summary>从 cURL 导入请求</summary><label>cURL（bash）<textarea v-model="curlText" maxlength="16384" rows="5" placeholder="curl 'http://127.0.0.1:8000/api/products?page=1&amp;page_size=10'"></textarea></label><button type="button" @click="previewCurl" :disabled="previewBusy||!curlText.trim()">{{previewBusy?'解析中…':'解析并预览'}}</button><button v-if="curlText" type="button" @click="curlText='';curlPreview=null">清除导入</button><div v-if="curlPreview" class="curl-preview" role="status"><template v-if="curlPreview.executable"><strong>已切换为导入请求模式</strong><p>{{curlPreview.url}}</p><pre>{{JSON.stringify(curlPreview.query,null,2)}}</pre><small>创建任务将直接观察此接口，跳过页面地址与翻页按钮。字段、页数和 RAG 设置仍生效。</small><small v-for="note in curlPreview.notes" :key="note">{{note}}</small></template><p v-for="reason in curlPreview.reasons" :key="reason">{{reason}}</p></div><small>解析不执行请求、不调用模型。输入不会保存；创建任务仅保存通过校验的 URL。</small></details>
 <label>页面地址<input v-model="url" :disabled="!!curlPreview?.executable" type="url" required maxlength="512"></label>
 <label>提取字段<input v-model="fields" required maxlength="200"><small>多个字段用英文逗号分隔</small></label>
 <label>最大页数<input v-model.number="maxPages" type="number" min="1" max="10" required></label>
 <label>翻页按钮<input v-model="clickText" :disabled="!!curlPreview?.executable" maxlength="80"><small>用于在页面中定位翻页的按钮文本</small></label>
 <label class="check"><input v-model="rag" type="checkbox">使用案例 RAG</label><small class="rag-help">参考本地案例，可关闭进行对照。</small>
 <button class="primary" type="submit" :disabled="pending||previewBusy||!!active||!configured||!!error|| (!!curlText.trim()&&!curlPreview?.executable)">{{pending?'处理中…':'创建任务'}}</button>
 <p class="hint">{{active?'已有任务运行中，完成后可创建新任务。':!configured&&!loading?'模型未配置，请检查服务端环境变量。':'仅支持本机 HTTP 页面与 GET 页码分页。'}}</p>
 </form></section>
 <div class="workspace">
 <section class="panel"><div class="heading"><h2>任务历史</h2><span class="muted">共 {{total}} 个</span></div>
 <p v-if="loading">正在加载…</p><p v-else-if="!tasks.length" class="empty">还没有任务，从左侧创建第一个任务。</p>
 <div v-else class="table-scroll"><table><thead><tr><th>任务</th><th>状态</th><th>创建时间</th></tr></thead><tbody><tr v-for="task in tasks" :key="task.id" :class="{selected:route.params.taskId===task.id}"><td><RouterLink :to="'/'+task.id" :title="task.id">{{task.id.slice(0,8)}}</RouterLink></td><td><span class="status" :class="task.status">{{labels[task.status]||task.status}}</span></td><td>{{date(task.created_at)}}</td></tr></tbody></table></div>
 <nav v-if="total>5" class="pagination" aria-label="历史分页"><button :disabled="page===1" @click="changePage(-1)">上一页</button><span>{{page}} / {{Math.ceil(total/5)}}</span><button :disabled="page*5>=total" @click="changePage(1)">下一页</button></nav>
 </section>
 <section class="panel"><h2>任务详情</h2><p v-if="!selected" class="empty">{{route.params.taskId?'正在加载任务…':'选择任务查看执行详情。'}}</p>
 <template v-else><div class="heading detail-heading"><div><strong>{{selected.id.slice(0,8)}}</strong> <span class="status" :class="selected.status">{{labels[selected.status]}}</span></div><button v-if="!terminal.includes(selected.status)" :disabled="pending||selected.status==='cancelling'" @click="cancel">取消任务</button><span v-else class="muted">{{date(selected.created_at)}}</span></div>
 <ol class="steps"><li v-for="(s,i) in stages" :key="s" :class="{done:reached(s)}"><span>{{reached(s)?'✓':i+1}}</span>{{stageNames[i]}}</li></ol>
 <div class="metrics"><div><b>{{summary?.count??'—'}}</b>条数据</div><div><b>{{summary?.pages??'—'}}</b>页</div><div><b>{{summary?.model_calls??'—'}}</b>次模型调用</div></div>
 <p v-if="summary?.error" class="alert" role="status">任务{{labels[selected.status]}}：{{summary.error}}</p>
 <p v-if="summary?.completeness" class="hint">{{summary.completeness==='complete'?'完整性验证通过':summary.completeness==='partial'?'已达到页数上限，结果不完整':'已满足停止条件，总数尚未验证'}} · {{summary.elapsed_seconds}} 秒 · RAG {{selected.spec.rag_enabled===false?'关闭':summary.retrieval?.enabled?'开启':'未记录'}}</p>
 </template></section>
 <section class="panel"><h2>产物下载</h2><p v-if="!artifacts.length" class="empty">任务结束后，已生成的产物会显示在这里。</p><div v-else class="table-scroll"><table><thead><tr><th>文件名</th><th>操作</th></tr></thead><tbody><tr v-for="a in artifacts" :key="a.id"><td>{{a.filename}}</td><td><a :href="'/api/v1/artifacts/'+a.id+'/download'" :aria-label="'下载 '+a.filename">下载</a></td></tr></tbody></table></div></section>
 </div></div></main>
</template>
