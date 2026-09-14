import { createApp } from 'vue'
import { createRouter, createWebHashHistory } from 'vue-router'
import App from './App.vue'
import TaskTraceViewer from './trace/TaskTraceViewer.vue'
import { h } from 'vue'
import { RouterView } from 'vue-router'
import './style.css'
const router = createRouter({history:createWebHashHistory(),routes:[{path:'/:taskId/trace',component:TaskTraceViewer},{path:'/:taskId?',component:App}]})
createApp({ render: () => h(RouterView) }).use(router).mount('#app')
