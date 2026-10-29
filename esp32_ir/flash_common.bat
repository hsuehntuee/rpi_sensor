@echo off
rem 共用燒錄流程，由 flash_304D.bat / flash_614.bat 呼叫：flash_common.bat <env> <名稱>
chcp 65001 >nul
setlocal
cd /d "%~dp0firmware"

echo == 燒錄 %~2 的冷氣遙控 ESP32 韌體
echo    請先用 USB 線把 ESP32 接到這台電腦（只接一塊）
echo.

where python >nul 2>&1
if errorlevel 1 (
    echo [錯誤] 找不到 python，請先安裝 Python 3（安裝時勾選 Add to PATH）
    pause
    exit /b 1
)

python -c "import platformio" >nul 2>&1
if errorlevel 1 (
    echo == 第一次使用，安裝 platformio
    python -m pip install platformio
    if errorlevel 1 (
        echo [錯誤] platformio 安裝失敗，請看上面的訊息
        pause
        exit /b 1
    )
)

echo == 編譯並燒錄（第一次約 10 分鐘，之後約 30 秒）
python -m platformio run -e %1 -t upload
if errorlevel 1 (
    echo.
    echo [失敗] 常見原因：
    echo   1. USB 線只能充電不能傳資料，換一條
    echo   2. 裝置管理員沒有 CH340 / CP210x 的 COM port，要裝驅動
    echo   3. 卡在 Connecting... 時，按住板子上的 BOOT 鍵直到開始寫入
    pause
    exit /b 1
)

echo.
echo == 完成！這塊 ESP32 已寫好 %~2 的冷氣協定，拔下來插到 %~2 的樹莓派即可
pause
