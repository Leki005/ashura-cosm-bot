$root = $PSScriptRoot
$zip  = "$root\bot.zip"
$tmp  = "$root\_pack_tmp"

Remove-Item $tmp, $zip -Recurse -Force -ErrorAction SilentlyContinue
$project = "$tmp\cosmetology_bot"
New-Item -ItemType Directory -Path $project, "$project\data", "$project\logs" | Out-Null

$skip = @("venv", ".venv", "__pycache__", ".git", "_pack_tmp", "bot.zip", ".env", "deploy.local", "_server.env", "backups", "logs", "bot.db", "bot.db.backup_*", "*.db", ".env.bak", "deploy.local*")
Get-ChildItem $root -Force | Where-Object { $_.Name -notin $skip } | ForEach-Object {
    Copy-Item $_.FullName -Destination $project -Recurse -Force
}
Get-ChildItem $project -Recurse -Directory -Filter "__pycache__" | Remove-Item -Recurse -Force
# bot.db НЕ копируем — на сервере своя БД (data/bot.db через Docker volume)
# if (Test-Path "$root\bot.db") { Copy-Item "$root\bot.db" "$project\data\bot.db" }

# В архиве: cosmetology_bot/... (для /opt/deploy.sh)
Compress-Archive -Path "$tmp\cosmetology_bot" -DestinationPath $zip
Remove-Item $tmp -Recurse -Force
Write-Host "Готово: $zip"