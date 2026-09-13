import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'
export default defineConfig({plugins:[vue()],base:'/console/',server:{proxy:{'/api':{target:'http://127.0.0.1:8002'},'/docs':{target:'http://127.0.0.1:8002'},'/openapi.json':{target:'http://127.0.0.1:8002'}}}})
