@echo off
chcp 65001 > nul
echo ========================================
echo  GOLD AI トレーダー 起動中...
echo ========================================
cd /d "%~dp0"
python mt5_gold_trader.py
pause
