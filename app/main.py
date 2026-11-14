"""入口：只监听回环地址，由 Caddy 反代对外。

用法：python -m app.main    （或 systemd 里的 ExecStart）
"""
from __future__ import annotations

import os

import uvicorn

if __name__ == "__main__":
    host = os.environ.get("DROPS_HOST", "127.0.0.1")
    port = int(os.environ.get("DROPS_PORT", "9200"))
    uvicorn.run(
        "app.web:app",
        host=host,
        port=port,
        log_level=os.environ.get("DROPS_LOG_LEVEL", "info"),
        access_log=False,
        proxy_headers=True,
        forwarded_allow_ips="127.0.0.1",
    )
