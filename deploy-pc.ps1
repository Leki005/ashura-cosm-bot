# Деплой одной командой с ПК (SSH-ключ — без пароля каждый раз)
$ErrorActionPreference = "Stop"
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

function Get-SshKeyPath($cfg) {
    if ($cfg["SSH_KEY_PATH"] -and (Test-Path $cfg["SSH_KEY_PATH"])) {
        return $cfg["SSH_KEY_PATH"]
    }
    foreach ($c in @(
        "$env:USERPROFILE\.ssh\id_ed25519",
        "$env:USERPROFILE\.ssh\id_rsa"
    )) {
        if (Test-Path $c) { return $c }
    }
    return "$env:USERPROFILE\.ssh\id_ed25519"
}

function Test-SshKeyAuth($target, $keyPath) {
    $args = @(
        "-o", "BatchMode=yes",
        "-o", "ConnectTimeout=8",
        "-o", "StrictHostKeyChecking=accept-new"
    )
    if ((Test-Path $keyPath)) { $args += @("-i", $keyPath) }
    & ssh @args $target "exit 0" 2>$null
    return $LASTEXITCODE -eq 0
}

function Show-SshKeyGuide($target, $keyPath) {
    Write-Host ""
    Write-Host "  SSH-ключ не настроен — без него нужно вводить пароль каждый раз." -ForegroundColor Yellow
    Write-Host ""
    Write-Host "  Рекомендуем настроить ключ (пароль введёте только ОДИН раз):" -ForegroundColor Cyan
    Write-Host "    1. Скрипт создаст ключ: $keyPath"
    Write-Host "    2. Скопирует его на сервер: $target"
    Write-Host "    3. Дальше deploy-pc.bat работает без пароля"
    Write-Host ""
    Write-Host "  Пароль сервера НЕ сохраняется в файлы — только в памяти на секунду." -ForegroundColor DarkGray
    Write-Host ""
    Write-Host "  Вручную (альтернатива):" -ForegroundColor DarkGray
    Write-Host "    ssh-keygen -t ed25519"
    Write-Host ('    type ' + $env:USERPROFILE + '\.ssh\id_ed25519.pub | ssh ' + $target + ' "mkdir -p ~/.ssh; cat >> ~/.ssh/authorized_keys"')
    Write-Host ""
}

function Setup-SshKey($target, $keyPath) {
    $dir = Split-Path $keyPath -Parent
    if (-not (Test-Path $dir)) { New-Item -ItemType Directory -Path $dir -Force | Out-Null }
    if (-not (Test-Path $keyPath)) {
        Write-Host "      Создаю SSH-ключ..." -ForegroundColor Green
        & ssh-keygen -t ed25519 -f $keyPath -N '""' -q
        Write-Host "      Ключ создан: $keyPath"
    }
    $pub = "$keyPath.pub"
    if (-not (Test-Path $pub)) {
        Write-Host "ОШИБКА: не найден $pub" -ForegroundColor Red
        exit 1
    }
    Write-Host ""
    Write-Host "  Сейчас введите пароль от сервера (последний раз):" -ForegroundColor Yellow
    Get-Content $pub -Raw | ssh -o StrictHostKeyChecking=accept-new $target `
        'mkdir -p ~/.ssh; chmod 700 ~/.ssh; cat >> ~/.ssh/authorized_keys; chmod 600 ~/.ssh/authorized_keys'
    if ($LASTEXITCODE -ne 0) {
        Write-Host "ОШИБКА: не удалось установить ключ. Проверьте IP, логин и пароль." -ForegroundColor Red
        exit 1
    }
    Write-Host "      SSH-ключ установлен на сервер." -ForegroundColor Green
}

function Invoke-Ssh($target, $keyPath, [string[]]$RemoteCmd) {
    $args = @("-o", "StrictHostKeyChecking=accept-new")
    if (Test-Path $keyPath) { $args += @("-i", $keyPath) }
    $args += $target
    $args += $RemoteCmd
    & ssh @args
    if ($LASTEXITCODE -ne 0) { throw "SSH ошибка" }
}

function Invoke-Scp($local, $remote, $keyPath) {
    $args = @("-o", "StrictHostKeyChecking=accept-new")
    if (Test-Path $keyPath) { $args += @("-i", $keyPath) }
    $args += $local, $remote
    & scp @args
    if ($LASTEXITCODE -ne 0) { throw "SCP ошибка" }
}

function Get-PuTTYExe($name) {
    foreach ($p in @(
        "C:\Program Files\PuTTY\$name.exe",
        "C:\Program Files (x86)\PuTTY\$name.exe"
    )) {
        if (Test-Path $p) { return $p }
    }
    return $null
}

function Ensure-PlinkHostKey($host_, $user, $password) {
    $plink = Get-PuTTYExe "plink"
    if (-not $plink) { return $false }
    $target = "${user}@${host_}"
    $accepted = $false

    for ($try = 1; $try -le 10; $try++) {
        Write-Host "      Попытка подключения $try/10..." -ForegroundColor DarkGray
        & $plink -batch -ssh $target -pw $password "exit" 2>$null | Out-Null
        if ($LASTEXITCODE -eq 0) { return $true }

        if (-not $accepted) {
            "y" | & $plink -ssh $target -pw $password "exit" 2>$null | Out-Null
            if ($LASTEXITCODE -eq 0) { return $true }
            $accepted = $true
        }
        Start-Sleep -Seconds 5
    }
    return $false
}

function Setup-SshKeyPlink($host_, $user, $password, $keyPath) {
    $plink = Get-PuTTYExe "plink"
    $pscp  = Get-PuTTYExe "pscp"
    if (-not $plink -or -not $pscp) {
        Write-Host "ОШИБКА: нужен PuTTY (plink + pscp). Скачайте: https://www.putty.org/" -ForegroundColor Red
        exit 1
    }

    $dir = Split-Path $keyPath -Parent
    if (-not (Test-Path $dir)) { New-Item -ItemType Directory -Path $dir -Force | Out-Null }
    if (-not (Test-Path $keyPath)) {
        Write-Host "      Создаю SSH-ключ..." -ForegroundColor Green
        & ssh-keygen -t ed25519 -f $keyPath -N '""' -q
    }
    $pub = "$keyPath.pub"
    if (-not (Test-Path $pub)) {
        Write-Host "ОШИБКА: не найден $pub" -ForegroundColor Red
        exit 1
    }

    Write-Host "      Подключаюсь к серверу (пароль из deploy.local)..." -ForegroundColor Green
    if (-not (Ensure-PlinkHostKey $host_ $user $password)) {
        Write-Host ""
        Write-Host "ОШИБКА: не удалось подключиться к серверу $host_" -ForegroundColor Red
        Write-Host ""
        Write-Host "  Пароль здесь ни при чём — SSH-сессия обрывается на сервере." -ForegroundColor Yellow
        Write-Host "  1) Запустите на рабочем столе: ПРОВЕРИТЬ СЕРВЕР.bat" -ForegroundColor Yellow
        Write-Host "  2) В панели хостинга откройте веб-консоль и выполните: systemctl restart ssh" -ForegroundColor Yellow
        Write-Host "  3) Подождите 2-3 минуты и снова: ЗАДЕПЛОИТЬ БОТА.bat" -ForegroundColor Yellow
        Write-Host ""
        exit 1
    }

    $target = "${user}@${host_}"
    & $pscp -batch -pw $password $pub "${target}:/tmp/ashura_install_key.pub"
    if ($LASTEXITCODE -ne 0) {
        Write-Host "ОШИБКА: не удалось загрузить ключ на сервер." -ForegroundColor Red
        exit 1
    }

    $cmd = 'mkdir -p ~/.ssh; chmod 700 ~/.ssh; cat /tmp/ashura_install_key.pub >> ~/.ssh/authorized_keys; chmod 600 ~/.ssh/authorized_keys; rm -f /tmp/ashura_install_key.pub'
    & $plink -batch -ssh $target -pw $password $cmd
    if ($LASTEXITCODE -ne 0) {
        Write-Host "ОШИБКА: не удалось установить SSH-ключ на сервере." -ForegroundColor Red
        exit 1
    }
    Write-Host "      SSH-ключ установлен — пароль больше не нужен." -ForegroundColor Green
}

function Invoke-SshPassword($host_, $user, $password, [string]$RemoteCmd) {
    $plink = Get-PuTTYExe "plink"
    if (-not $plink) { throw "PuTTY plink не найден" }
    if (-not (Ensure-PlinkHostKey $host_ $user $password)) { throw "Не удалось подключиться к серверу" }
    & $plink -batch -ssh "${user}@${host_}" -pw $password $RemoteCmd
    if ($LASTEXITCODE -ne 0) { throw "SSH ошибка (plink)" }
}

function Invoke-ScpPassword($local, $host_, $user, $password, $remotePath) {
    $pscp = Get-PuTTYExe "pscp"
    if (-not $pscp) { throw "PuTTY pscp не найден" }
    if (-not (Ensure-PlinkHostKey $host_ $user $password)) { throw "Не удалось подключиться к серверу" }
    & $pscp -batch -pw $password $local "${user}@${host_}:$remotePath"
    if ($LASTEXITCODE -ne 0) { throw "SCP ошибка (pscp)" }
}

# ========== СТАРТ ==========
Write-Host ""
Write-Host "==========================================" -ForegroundColor Cyan
Write-Host "   Деплой AshuraCosm Bot с вашего ПК"
Write-Host "==========================================" -ForegroundColor Cyan
Write-Host ""

if (-not (Get-Command ssh -ErrorAction SilentlyContinue)) {
    Write-Host "ОШИБКА: нет OpenSSH. Установите:" -ForegroundColor Red
    Write-Host "  Параметры Windows → Приложения → OpenSSH Client" -ForegroundColor Yellow
    exit 1
}

# --- deploy.local ---
$localFile = Join-Path $root "deploy.local"
if (-not (Test-Path $localFile)) {
    Copy-Item (Join-Path $root "deploy.local.example") $localFile
    Write-Host "[Настройка] Создан deploy.local" -ForegroundColor Yellow
    Write-Host "  Укажите IP сервера (SERVER_HOST), сохраните файл." -ForegroundColor Yellow
    notepad $localFile
    Write-Host "  Запустите deploy-pc.bat снова." -ForegroundColor Yellow
    exit 0
}
$cfg = Read-Config $localFile
$host_ = $cfg["SERVER_HOST"]
$user  = $cfg["SERVER_USER"]
$path  = $cfg["SERVER_PATH"]
if (-not $host_ -or $host_ -match '123\.456' -or -not $user -or -not $path) {
    Write-Host "ОШИБКА: укажите реальный SERVER_HOST в deploy.local" -ForegroundColor Red
    notepad $localFile
    exit 1
}
$target = "${user}@${host_}"
$keyPath = Get-SshKeyPath $cfg
$password = $cfg["SERVER_PASSWORD"]
$usePassword = [bool]$password
$optDeploySh = Join-Path (Split-Path $root -Parent) "server-opt\deploy.sh"

# --- .env ---
$envFile = Join-Path $root ".env"
if (-not (Test-Path $envFile)) {
    Copy-Item (Join-Path $root ".env.example") $envFile
    Write-Host "[Настройка] Создан .env — заполните BOT_TOKEN и ADMIN_ID" -ForegroundColor Yellow
    notepad $envFile
    Write-Host "  Запустите deploy-pc.bat снова." -ForegroundColor Yellow
    exit 0
}
$envText = Get-Content $envFile -Raw -Encoding UTF8
if ($envText -match 'BOT_TOKEN=your_' -or $envText -notmatch 'BOT_TOKEN=\S+') {
    Write-Host "ОШИБКА: заполните BOT_TOKEN в .env" -ForegroundColor Red
    notepad $envFile
    exit 1
}
if ($envText -notmatch 'ADMIN_ID=\d+') {
    Write-Host "ОШИБКА: заполните ADMIN_ID (число) в .env" -ForegroundColor Red
    notepad $envFile
    exit 1
}

# --- SSH-ключ ---
Write-Host "[Подготовка] Проверка подключения к серверу..." -ForegroundColor Green
$hasKeyAuth = Test-SshKeyAuth $target $keyPath
if (-not $hasKeyAuth) {
    if ($usePassword) {
        Write-Host "      Пароль найден в deploy.local — настраиваю SSH-ключ автоматически..." -ForegroundColor Green
        Setup-SshKeyPlink $host_ $user $password $keyPath
        $hasKeyAuth = Test-SshKeyAuth $target $keyPath
        if (-not $hasKeyAuth) {
            Write-Host "      Ключ не применился — буду использовать пароль через PuTTY." -ForegroundColor Yellow
        }
    } else {
        Show-SshKeyGuide $target $keyPath
        $ans = Read-Host "  Настроить SSH-ключ сейчас? (Y/n)"
        if ($ans -eq 'n' -or $ans -eq 'N') {
            Write-Host "  ОШИБКА: добавьте SERVER_PASSWORD в deploy.local или настройте SSH-ключ." -ForegroundColor Red
            exit 1
        } else {
            Setup-SshKey $target $keyPath
            $hasKeyAuth = Test-SshKeyAuth $target $keyPath
            if (-not $hasKeyAuth) {
                Write-Host "ОШИБКА: ключ не работает. Повторите настройку." -ForegroundColor Red
                exit 1
            }
        }
    }
}
if ($hasKeyAuth) {
    Write-Host "      SSH-ключ работает — пароль вводить не нужно." -ForegroundColor Green
} elseif ($usePassword) {
    Write-Host "      Подключение по паролю через PuTTY (без ручного ввода)." -ForegroundColor Green
} else {
    Write-Host "ОШИБКА: нет способа подключиться к серверу." -ForegroundColor Red
    exit 1
}

Write-Host ""
Write-Host "  Сервер: $target"
Write-Host "  Папка:  $path"
Write-Host ""

# --- [1/4] Упаковка ---
Write-Host "[1/4] Упаковка bot.zip..." -ForegroundColor Green
& (Join-Path $root "pack.ps1")

# --- [2/4] .env для сервера ---
Write-Host "[2/4] .env для сервера (PROXY_AUTO=false)..." -ForegroundColor Green
$serverEnv = Join-Path $root "_server.env"
$out = @()
$hasProxy = $false
foreach ($line in (Get-Content $envFile -Encoding UTF8)) {
    if ($line -match '^\s*PROXY_AUTO=') { $out += "PROXY_AUTO=false"; $hasProxy = $true }
    else { $out += $line }
}
if (-not $hasProxy) { $out += "PROXY_AUTO=false" }
Set-Content $serverEnv $out -Encoding UTF8

# --- [3/4] Загрузка ---
Write-Host "[3/4] Загрузка на сервер..." -ForegroundColor Green
if ($hasKeyAuth) {
    Invoke-Ssh $target $keyPath @("mkdir -p /opt /opt/ashura-bot $path")
    Invoke-Scp (Join-Path $root "bot.zip") "${target}:/opt/bot.zip" $keyPath
    if (Test-Path $optDeploySh) {
        Invoke-Scp $optDeploySh "${target}:/opt/deploy.sh" $keyPath
    }
    Invoke-Scp $serverEnv "${target}:/opt/ashura-bot/.env" $keyPath
} else {
    Invoke-SshPassword $host_ $user $password "mkdir -p /opt /opt/ashura-bot $path"
    Invoke-ScpPassword (Join-Path $root "bot.zip") $host_ $user $password "/opt/bot.zip"
    if (Test-Path $optDeploySh) {
        Invoke-ScpPassword $optDeploySh $host_ $user $password "/opt/deploy.sh"
    }
    Invoke-ScpPassword $serverEnv $host_ $user $password "/opt/ashura-bot/.env"
}
Remove-Item $serverEnv -Force -ErrorAction SilentlyContinue
Write-Host "      Файлы загружены."

# --- [4/4] Запуск ---
Write-Host "[4/4] Запуск бота на сервере..." -ForegroundColor Green
if ($hasKeyAuth) {
    if (Test-Path $optDeploySh) {
        Invoke-Ssh $target $keyPath @("chmod +x /opt/deploy.sh; bash /opt/deploy.sh")
    } else {
        Invoke-Ssh $target $keyPath @("cd $path; bash deploy.sh")
    }
} else {
    if (Test-Path $optDeploySh) {
        Invoke-SshPassword $host_ $user $password "chmod +x /opt/deploy.sh; bash /opt/deploy.sh"
    } else {
        Invoke-SshPassword $host_ $user $password "cd $path; bash deploy.sh"
    }
}

Write-Host ""
Write-Host "==========================================" -ForegroundColor Green
Write-Host "  Готово! Проверьте /start в Telegram"
Write-Host "==========================================" -ForegroundColor Green
Write-Host ""