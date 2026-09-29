@echo off
chcp 65001 > nul
pwsh -ExecutionPolicy Bypass -Command ^
  "$ErrorActionPreference='Stop';" ^
  "$MOBILE='C:\Users\user\Documents\ai-trading-system\mobile-app';" ^
  "$ANDROID=\"$MOBILE\android\";" ^
  "$APK=\"$ANDROID\app\build\outputs\apk\debug\app-debug.apk\";" ^
  "Write-Host '[ 1/4 frontend build ]' -ForegroundColor Cyan;" ^
  "Set-Location $MOBILE; npm run build;" ^
  "Write-Host '[ 2/4 cap sync ]' -ForegroundColor Cyan;" ^
  "npx cap sync android;" ^
  "Write-Host '[ 3/4 gradle ]' -ForegroundColor Cyan;" ^
  "Set-Location $ANDROID; .\gradlew assembleDebug;" ^
  "Write-Host '[ 4/4 ADB install ]' -ForegroundColor Cyan;" ^
  "adb connect 192.168.0.86:5555;" ^
  "adb -s 192.168.0.86:5555 install -r $APK;" ^
  "Write-Host 'Done! Installed on phone.' -ForegroundColor Green;" ^
  "Read-Host 'Press Enter to close'"
