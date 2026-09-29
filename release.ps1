$ErrorActionPreference = "Stop"

$ROOT    = "C:\Users\user\Documents\ai-trading-system"
$MOBILE  = "$ROOT\mobile-app"
$ANDROID = "$MOBILE\android"
$GRADLE  = "$ANDROID\app\build.gradle"
$APK     = "$ANDROID\app\build\outputs\apk\debug\app-debug.apk"
$REPO    = "kisshi021920hm-hash/ai-trading-system"

function Step($msg) { Write-Host "" ; Write-Host "[ $msg ]" -ForegroundColor Cyan }
function Ok($msg)   { Write-Host "  OK: $msg" -ForegroundColor Green }
function Fail($msg) { Write-Host "  NG: $msg" -ForegroundColor Red; Read-Host "Enter"; exit 1 }

Write-Host "================================================" -ForegroundColor Yellow
Write-Host "  GOLD AI Trader  Release Builder" -ForegroundColor Yellow
Write-Host "================================================" -ForegroundColor Yellow

# ---- 1: version increment ----
Step "Version update"
$utf8noBom = New-Object System.Text.UTF8Encoding $false
$raw       = [System.IO.File]::ReadAllText($GRADLE, $utf8noBom)
$oldCode   = [int]([regex]::Match($raw, 'versionCode\s+(\d+)').Groups[1].Value)
$oldName   = [regex]::Match($raw, 'versionName\s+"([^"]+)"').Groups[1].Value
if (-not $oldCode -or -not $oldName) { Fail "build.gradle parse error" }

$newCode   = $oldCode + 1
$parts     = $oldName.Split('.')
$parts[-1] = [string]([int]$parts[-1] + 1)
$newName   = $parts -join '.'

$raw = $raw -replace "versionCode\s+$oldCode",                          "versionCode $newCode"
$raw = $raw -replace "versionName\s+`"$([regex]::Escape($oldName))`"", "versionName `"$newName`""
[System.IO.File]::WriteAllText($GRADLE, $raw, $utf8noBom)
Ok "$oldName ($oldCode) -> $newName ($newCode)"

# App.tsxのAPP_VERSIONも更新
$appTsx = "$MOBILE\src\App.tsx"
$tsxRaw = [System.IO.File]::ReadAllText($appTsx, $utf8noBom)
$tsxRaw = $tsxRaw -replace 'const APP_VERSION = "[^"]*"', "const APP_VERSION = `"$newName`""
[System.IO.File]::WriteAllText($appTsx, $tsxRaw, $utf8noBom)
Ok "App.tsx APP_VERSION -> $newName"

# ---- 2: frontend build ----
Step "npm run build"
Set-Location $MOBILE
npm run build
if ($LASTEXITCODE -ne 0) { Fail "npm run build failed" }
Ok "done"

# ---- 3: cap sync ----
Step "cap sync android"
npx cap sync android
if ($LASTEXITCODE -ne 0) { Fail "cap sync failed" }
Ok "done"

# ---- 4: gradle ----
Step "gradlew assembleDebug"
Set-Location $ANDROID
.\gradlew assembleDebug
if ($LASTEXITCODE -ne 0) { Fail "gradle build failed" }
Ok "APK: $APK"

# ---- 5: git commit version bump ----
Step "git commit (version bump)"
Set-Location $ROOT
git add mobile-app/android/app/build.gradle mobile-app/src/App.tsx
git commit -m "chore: bump version to $newName (versionCode $newCode)

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>"
Ok "committed"

# ---- 6: GitHub Release ----
Step "GitHub Release: v$newName"
$TAG   = "v$newName"
$DATE  = Get-Date -Format "yyyy/MM/dd HH:mm"
$NOTES = "## GOLD AI Trader $newName`n`nbuild: $DATE  versionCode: $newCode`n`n**Install**`n1. Tap ``app-debug.apk`` below`n2. Allow unknown sources`n3. Install"

gh release create $TAG $APK --repo $REPO --title "GOLD AI Trader $newName" --notes $NOTES --latest
if ($LASTEXITCODE -ne 0) { Fail "gh release create failed" }
Ok "Release created"

# ---- done ----
$URL = "https://github.com/$REPO/releases/latest"
Write-Host ""
Write-Host "================================================" -ForegroundColor Green
Write-Host "  Release complete!  $newName (code $newCode)" -ForegroundColor Green
Write-Host "  $URL" -ForegroundColor White
Write-Host "================================================" -ForegroundColor Green
Write-Host ""
Write-Host "Open the URL above on your phone to download APK." -ForegroundColor Yellow

Set-Location $ROOT
Read-Host "Press Enter to close"
