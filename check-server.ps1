# Проверка доступности сервера перед деплоем
$ErrorActionPreference = "Continue"
$root = $PSScriptRoot

function Read-Config($path) {
    $cfg = @{}
    Get-Content $path -Encoding UTF8 | ForEach-Object {
        if ($_ -match '^\s*#' -or $_ -notmatch '=') { return }
        $p = $_ -split '=', 2
        $cfg[$p[0].Trim()] = $p[1].Trim()
    }
    return $cfg
}

Write-Host ""
Write-Host "==========================================" -ForegroundColor Cyan
Write-Host "   Проверка сервера"
Write-Host "==========================================" -ForegroundColor Cyan
Write-Host ""

$localFile = Join-Path $root "deploy.local"
if (-not (Test-Path $localFile)) {
    Write-Host "ОШИБКА: не найден deploy.local" -ForegroundColor Red
    exit 1
}

$cfg = Read-Config $localFile
$host_ = $cfg["SERVER_HOST"]
$user  = $cfg["SERVER_USER"]
$password = $cfg["SERVER_PASSWORD"]

if (-not $host_) {
    Write-Host "ОШИБКА: укажите SERVER_HOST в deploy.local" -ForegroundColor Red
    exit 1
}

Write-Host "[1/3] Проверка порта 22 (TCP)..." -ForegroundColor Green
$tcp = Test-NetConnection -ComputerName $host_ -Port 22 -WarningAction SilentlyContinue
if ($tcp.TcpTestSucceeded) {
    Write-Host "      Порт 22 открыт — сервер отвечает." -ForegroundColor Green
} else {
    Write-Host "      Порт 22 НЕ доступен." -ForegroundColor Red
    Write-Host "      Сервер выключен или IP неверный." -ForegroundColor Yellow
    exit 1
}

Write-Host ""
Write-Host "[2/3] Проверка SSH-подключения..." -ForegroundColor Green
$plink = "C:\Program Files\PuTTY\plink.exe"
if (-not (Test-Path $plink)) {
    Write-Host "      PuTTY не найден. Установите: https://www.putty.org/" -ForegroundColor Red
    exit 1
}

$target = "${user}@${host_}"
$ok = $false
for ($i = 1; $i -le 3; $i++) {
    Write-Host "      Попытка $i из 3..." -ForegroundColor DarkGray
    if ($password) {
        & $plink -batch -ssh $target -pw $password "echo CONNECTED" 2>$null | Out-Null
    } else {
        & $plink -batch -ssh $target "echo CONNECTED" 2>$null | Out-Null
    }
    if ($LASTEXITCODE -eq 0) {
        $ok = $true
        break
    }
    Start-Sleep -Seconds 3
}

if ($ok) {
    Write-Host "      SSH работает! Можно деплоить." -ForegroundColor Green
    Write-Host ""
    Write-Host "[3/3] Готово — запускайте ЗАДЕПЛОИТЬ БОТА.bat" -ForegroundColor Green
    exit 0
}

Write-Host "      SSH НЕ работает (соединение обрывается)." -ForegroundColor Red
Write-Host ""
Write-Host "[3/3] Что делать:" -ForegroundColor Yellow
Write-Host "  1. Зайдите в панель хостинга (сайт, где покупали VPS)"
Write-Host "  2. Откройте веб-консоль (VNC / Serial console)"
Write-Host "  3. Выполните в консоли:"
Write-Host "       systemctl restart ssh"
Write-Host "  4. Если не помогло:"
Write-Host "       fail2ban-client status sshd"
Write-Host "  5. Подождите 2-3 минуты и запустите эту проверку снова"
Write-Host ""
Write-Host "  IP сервера: $host_" -ForegroundColor DarkGray
Write-Host ""
exit 1