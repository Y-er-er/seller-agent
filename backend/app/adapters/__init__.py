"""适配器实现层。

⚠️ **业务代码不要直接 import 本包** —— 一律经 `app.ports` 取实例。
本包内的模块只被 `app/ports/__init__.py`（工厂）与测试引用。

| 模块 | 端口 | 说明 |
| --- | --- | --- |
| `mapping.py` | — | 外部原始数据 → 领域模型的**唯一一处**映射 |
| `datasource_mock.py` | DataSource | 进程内夹具（开发 / 演示 / 离线测试） |
| `datasource_http.py` | DataSource | 真实只读 HTTP（生产） |
| `storage_local.py` | ObjectStorage | 本地目录（开发） |
| （待建）`storage_cos.py` | ObjectStorage | 腾讯云 COS（生产，本轮未实现） |
"""
