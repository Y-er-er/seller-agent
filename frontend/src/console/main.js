import { createApp } from 'vue'
import App from './App.vue'
import '../../console.css'

// ?embed=1：被卖家控制台以 iframe 内嵌时隐藏自身顶栏（CSS 依据 html.embed 判定）
if (new URLSearchParams(location.search).get('embed') === '1') {
  document.documentElement.classList.add('embed')
}

createApp(App).mount('#app')
