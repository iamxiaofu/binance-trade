"""web 包：仓位可视化的只读接口占位（SPEC「先留接口」）。

当前仅提供从 SQLite 读取状态的纯函数（status.py），不含 HTTP 服务/前端。
后期可在此基础上接 FastAPI + 开源仓位可视化前端。
"""
from web.status import status_summary  # noqa: F401
