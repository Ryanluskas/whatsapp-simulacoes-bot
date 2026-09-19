<#
    Desinstalador do Allana Bot.

    Remove o programa, os atalhos e o registro. Os DADOS (banco, comprovantes e
    perfis do navegador, que guardam a sessao do WhatsApp) so' somem se voce
    disser que sim -- eles nao existem em outro lugar.
#>
[CmdletBinding()]
param(
    [string]$Destino = "",
    [switch]$Silencioso,
    # Apaga banco, comprovantes e perfis sem perguntar. Nao ha' volta.
    [switch]$ApagarDados
)

$ErrorActionPreference = "Continue"
$chave = "HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\AllanaBot"

if (-not $Destino) {
    try { $Destino = (Get-ItemProperty $chave -ErrorAction Stop).InstallLocation } catch {}
}
if (-not $Destino) { $Destino = Split-Path -Parent $PSScriptRoot }

Write-Host ""
Write-Host "  Allana Bot - desinstalacao" -ForegroundColor White
Write-Host "  pasta: $Destino"

if (-not (Test-Path $Destino)) {
    Write-Host "  Essa pasta nao existe mais; removendo so' o registro." -ForegroundColor Yellow
    Remove-Item $chave -Recurse -Force -ErrorAction SilentlyContinue
    if (-not $Silencioso) { Read-Host "  Enter para fechar" | Out-Null }
    exit 0
}

# O bot em execucao segura o banco e a pasta; sem parar, a remocao falha pela metade.
$rodando = Get-CimInstance Win32_Process -Filter "Name = 'python.exe'" -ErrorAction SilentlyContinue |
    Where-Object { $_.CommandLine -and $_.CommandLine -like "*$Destino*" }
if ($rodando) {
    Write-Host "  O bot esta rodando; encerrando..." -ForegroundColor Yellow
    $rodando | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
    Start-Sleep -Seconds 2
}

$apagar = $ApagarDados
if (-not $apagar -and -not $Silencioso) {
    Write-Host ""
    Write-Host "  Os dados ficam em:" -ForegroundColor White
    Write-Host "   $Destino\dados          (banco: solicitacoes, consultores, logs)"
    Write-Host "   $Destino\comprovantes   (imagens enviadas no grupo)"
    Write-Host "   $Destino\perfis         (sessao do WhatsApp Web)"
    $resposta = Read-Host "  Apagar TAMBEM esses dados? (digite APAGAR para confirmar)"
    $apagar = ($resposta -eq "APAGAR")
}

$guardados = $null
if (-not $apagar) {
    $guardados = Join-Path ([Environment]::GetFolderPath("MyDocuments")) "AllanaBot-dados"
    New-Item -ItemType Directory -Force -Path $guardados | Out-Null
    foreach ($pasta in @("dados", "comprovantes", "perfis")) {
        $origem = Join-Path $Destino $pasta
        if (Test-Path $origem) { Move-Item $origem (Join-Path $guardados $pasta) -Force -ErrorAction SilentlyContinue }
    }
    $env_path = Join-Path $Destino ".env"
    if (Test-Path $env_path) { Move-Item $env_path (Join-Path $guardados ".env") -Force -ErrorAction SilentlyContinue }
}

foreach ($pasta in @(
    (Join-Path $env:APPDATA "Microsoft\Windows\Start Menu\Programs"),
    [Environment]::GetFolderPath("Desktop"),
    (Join-Path $env:USERPROFILE "OneDrive\Desktop")
)) {
    $lnk = Join-Path $pasta "Allana Bot.lnk"
    if (Test-Path $lnk) { Remove-Item $lnk -Force -ErrorAction SilentlyContinue }
}

Remove-Item $chave -Recurse -Force -ErrorAction SilentlyContinue

# O desinstalador vive dentro da pasta que ele apaga: sai de la' antes.
Set-Location ([Environment]::GetFolderPath("MyDocuments"))
Remove-Item $Destino -Recurse -Force -ErrorAction SilentlyContinue

Write-Host ""
if (Test-Path $Destino) {
    Write-Host "  Sobrou coisa em $Destino (arquivo em uso?). Apague a pasta na mao." -ForegroundColor Yellow
} else {
    Write-Host "  Removido." -ForegroundColor Green
}
if ($guardados) { Write-Host "  Seus dados foram guardados em: $guardados" -ForegroundColor White }
Write-Host ""
if (-not $Silencioso) { Read-Host "  Enter para fechar" | Out-Null }
