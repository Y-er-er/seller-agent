# legacy/ —— Vue 迁移前的免构建实现

这两个文件是 h5 买家侧与 console 售后指挥台在 **Vue 3 迁移前**的原生 JavaScript 实现。

| 文件 | 原入口 | 现实现 |
| --- | --- | --- |
| `h5.js` | `h5.html` | `src/h5/App.vue` |
| `console.js` | `console.html` | `src/console/App.vue` |

## 为什么保留

1. 它们仍是 `verify_h5.cjs` / `verify_console.cjs` 的回归基线 —— 两个脚本通过 `vm`
   执行这些文件，验证渲染纯函数与 API 路径契约。
2. 迁移期可作为行为对照与回退参照。

## 注意

- **不再被任何 HTML 引用**，也不参与 Vite 构建。
- 新实现的行为契约（SSE 分段、终态出口、人在环动作、演示模式标注）以 `src/` 为准。
- 待 Vue 版本的自动化验证补齐后，本目录可整体删除。
