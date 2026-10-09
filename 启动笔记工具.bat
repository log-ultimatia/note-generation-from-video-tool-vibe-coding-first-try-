@echo off
rem ============================================================
rem  课程视频 -> PDF 笔记   图形界面启动器
rem  双击本文件即可，不需要命令行。
rem ============================================================
cd /d "%~dp0"

rem 优先用 pythonw.exe：它是无控制台版的 Python，不会弹出黑色窗口
where pythonw.exe >nul 2>nul
if %errorlevel%==0 (
    start "" pythonw.exe "%~dp0app.py"
    goto :eof
)

rem 退而求其次用 python.exe（会带一个控制台窗口）
where python.exe >nul 2>nul
if %errorlevel%==0 (
    python "%~dp0app.py"
    if errorlevel 1 (
        echo.
        echo 程序异常退出，请把上面的错误信息截图反馈。
        pause
    )
    goto :eof
)

echo.
echo 没有找到 Python。
echo 请确认已安装 Python 3 并把它加入了 PATH，或直接运行：
echo     python "%~dp0app.py"
echo.
pause
