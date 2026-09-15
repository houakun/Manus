#!/bin/bash

# 启用uvicorn运行服务（使用exec让uvicorn成为主进程）
# 将stderr重定向到stdout，方便docker logs捕获所有错误信息
exec uvicorn app.main:app --host 0.0.0.0 --port 8000 --timeout-graceful-shutdown 5 --log-level debug 2>&1