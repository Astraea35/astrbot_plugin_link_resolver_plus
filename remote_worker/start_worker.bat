@echo off
chcp 65001 >nul
title 链接解析插件远程算力节点 (FastAPI Worker)

echo ==================================================
echo   正在检查运行环境...
echo ==================================================

python --version >nul 2>&1
if %errorlevel% neq 0 (
    echo [错误] 未检测到系统中的 Python，请先安装 Python 3.10+ 并添加至环境变量！
    pause
    exit /b 1
)

echo 正在检查并补齐 Python 依赖包 (fastapi, uvicorn, python-multipart 等)...
python -m pip install -r requirements.txt --quiet
if %errorlevel% neq 0 (
    echo [警告] 依赖安装可能遇到网络异常，尝试直接启动...
)

echo.
echo ==================================================
echo   启动算力服务中...
echo ==================================================
python server.py

if %errorlevel% neq 0 (
    echo [错误] 服务异常退出！
    pause
)
