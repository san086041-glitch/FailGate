"""维护者控制台（原计划 FastAPI + HTMX，M4 实现；M0 只有 failgate/api.py 的只读 JSON 接口）。

W12 最小版（ADR 0035）：改为一个纯静态页面 index.html（原生 JS 调 /api 的只读接口），
不引入模板引擎和前端构建；由 failgate/api.py 的 /console 提供。
"""
