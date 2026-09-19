<#
    Instalador do Allana Bot para Windows.

    Roda sem privilegio de administrador: tudo vai para a pasta do usuario.
    O que ele faz, nesta ordem:

      1. copia os arquivos para o destino;
      2. garante um Python 3.11+ (usa o que existir; se nao houver, baixa);
      3. cria a .venv e instala as dependencias;
      4. baixa o Chromium do Playwright;
      5. pergunta senha do painel, grupo do WhatsApp e porta;
      6. escreve o .env com um SESSION_SECRET novo (nunca vem pronto);
      7. cria atalhos e registra o desinstalador.

    Nada de segredo viaja no pacote: senha e SESSION_SECRET nascem aqui, nesta
    maquina. As credenciais do Santander ficam em arqueiro\credenciais.ini, que
    o instalador so' cria como exemplo.
#>
[CmdletBinding()]
param(
    [string]$Destino = "$env:LOCALAPPDATA\AllanaBot",
    [int]$Porta = 8000,
    [string]$Grupo = "",
    [string]$Senha = "",
    # Instalacao sem perguntas, para teste automatizado.
    [switch]$SemPerguntas,
    # Nao baixa Chromium nem instala Python (teste rapido em maquina ja pronta).
    [switch]$SemNavegador
)

$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"

$PYTHON_MINIMO = [version]"3.11"
$PYTHON_URL = "https://www.python.org/ftp/python/3.11.9/python-3.11.9-amd64.exe"
$Aqui = $PSScriptRoot
if (-not $Aqui) { $Aqui = Split-Path -Parent $MyInvocation.MyCommand.Path }
$Origem = Split-Path -Parent $Aqui   # o payload extraido fica um nivel acima

function Passo($texto) { Write-Host "`n>> $texto" -ForegroundColor Cyan }
function Ok($texto) { Write-Host "   $texto" -ForegroundColor Green }
function Aviso($texto) { Write-Host "   $texto" -ForegroundColor Yellow }

function Achar-Python {
    <# Devolve o caminho de um python 3.11+ ou $null. #>
    $candidatos = @()
    if (Get-Command py -ErrorAction SilentlyContinue) {
        $candidatos += @("py -3.12", "py -3.11", "py -3")
    }
    $candidatos += @("python")
    foreach ($c in $candidatos) {
        $partes = $c.Split(" ")
        $exe = $partes[0]
        $args = @()
        if ($partes.Count -gt 1) { $args = $partes[1..($partes.Count - 1)] }
        try {
            $saida = & $exe @args -c "import sys; print(sys.executable); print('%d.%d' % sys.version_info[:2])" 2>$null
        } catch { continue }
        if ($LASTEXITCODE -ne 0 -or -not $saida) { continue }
        $caminho = $saida[0]
        $versao = [version]$saida[1]
        if ($versao -ge $PYTHON_MINIMO) { return $caminho }
    }
    return $null
}

function Instalar-Python {
    Passo "Python 3.11 nao encontrado; baixando do python.org"
    $instalador = Join-Path $env:TEMP "python-3.11.9-amd64.exe"
    Invoke-WebRequest -Uri $PYTHON_URL -OutFile $instalador -UseBasicParsing
    Ok "baixado; instalando so' para o seu usuario (sem admin)"
    $p = Start-Process -FilePath $instalador -Wait -PassThru -ArgumentList @(
        "/quiet", "InstallAllUsers=0", "PrependPath=1", "Include_pip=1",
        "Include_test=0", "Include_launcher=1", "SimpleInstall=1")
    Remove-Item $instalador -Force -ErrorAction SilentlyContinue
    if ($p.ExitCode -ne 0) { throw "a instalacao do Python falhou (codigo $($p.ExitCode))" }
    $env:Path = [Environment]::GetEnvironmentVariable("Path", "User") + ";" +
                [Environment]::GetEnvironmentVariable("Path", "Machine")
    $achado = Achar-Python
    if (-not $achado) { throw "instalei o Python mas nao consegui encontra-lo; reabra o terminal e rode de novo" }
    return $achado
}

function Segredo-Aleatorio([int]$bytes = 32) {
    $buffer = New-Object byte[] $bytes
    [System.Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($buffer)
    return -join ($buffer | ForEach-Object { $_.ToString("x2") })
}

function Copiar-Arquivos {
    Passo "Copiando os arquivos para $Destino"
    
    $rodando = Get-CimInstance Win32_Process -Filter "Name = 'python.exe'" -ErrorAction SilentlyContinue |
        Where-Object { $_.CommandLine -and $_.CommandLine -like "*$Destino*" }
    if ($rodando) {
        Aviso "O bot esta rodando; encerrando para atualizar..."
        $rodando | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
        Start-Sleep -Seconds 2
    }

    New-Item -ItemType Directory -Force -Path $Destino | Out-Null
    $ignorar = @(".venv", "dados", "perfis", "comprovantes", ".git")
    Get-ChildItem -LiteralPath $Origem -Force | Where-Object { $ignorar -notcontains $_.Name } | ForEach-Object {
        Copy-Item -LiteralPath $_.FullName -Destination $Destino -Recurse -Force
    }
    foreach ($pasta in @("dados", "perfis", "comprovantes")) {
        New-Item -ItemType Directory -Force -Path (Join-Path $Destino $pasta) | Out-Null
    }
    Ok "arquivos no lugar"
}

function Preparar-Venv($python) {
    Passo "Criando o ambiente Python e instalando as dependencias"
    $venv = Join-Path $Destino ".venv"
    if (-not (Test-Path (Join-Path $venv "Scripts\python.exe"))) {
        & $python -m venv $venv
        if ($LASTEXITCODE -ne 0) { throw "nao consegui criar a .venv" }
    }
    $venvPy = Join-Path $venv "Scripts\python.exe"
    & $venvPy -m pip install --upgrade pip --quiet
    Write-Host "   instalando (isto leva alguns minutos)..." -ForegroundColor DarkGray
    & $venvPy -m pip install -r (Join-Path $Destino "requirements.txt") --quiet
    if ($LASTEXITCODE -ne 0) { throw "a instalacao das dependencias falhou" }
    Ok "dependencias instaladas"
    return $venvPy
}

function Instalar-Chromium($venvPy) {
    if ($SemNavegador) { Aviso "pulando o download do Chromium (-SemNavegador)"; return }
    Passo "Baixando o navegador do Playwright (Chromium, ~200 MB)"
    & $venvPy -m playwright install chromium
    if ($LASTEXITCODE -ne 0) { throw "o download do Chromium falhou" }
    Ok "navegador pronto"
}

function Perguntar-Configuracao {
    if (Test-Path (Join-Path $Destino ".env")) {
        Aviso "Instalacao existente detectada: pulando perguntas."
        return
    }
    
    if ($SemPerguntas) {
        if (-not $Senha) { $script:Senha = Segredo-Aleatorio 12 }
        if (-not $Grupo) { $script:Grupo = "Consultores" }
        return
    }
    Passo "Configuracao"
    while (-not $script:Senha -or $script:Senha.Length -lt 8) {
        $segura = Read-Host "   Senha do painel (minimo 8 caracteres)" -AsSecureString
        $script:Senha = [Runtime.InteropServices.Marshal]::PtrToStringAuto(
            [Runtime.InteropServices.Marshal]::SecureStringToBSTR($segura))
        if ($script:Senha.Length -lt 8) { Aviso "senha curta demais" }
    }
    if (-not $script:Grupo) {
        $resposta = Read-Host "   Nome do grupo do WhatsApp (Enter para 'Consultores')"
        if ($resposta) { $script:Grupo = $resposta } else { $script:Grupo = "Consultores" }
    }
    $resposta = Read-Host "   Porta do painel (Enter para $Porta)"
    if ($resposta) { $script:Porta = [int]$resposta }
}

function Escrever-Env {
    Passo "Escrevendo a configuracao (.env)"
    $env_path = Join-Path $Destino ".env"
    if (Test-Path $env_path) {
        Ok "O arquivo .env ja existe. Configuracao mantida."
        return
    }
    $linhas = @(
        "# Gerado pelo instalador em $(Get-Date -Format 'dd/MM/yyyy HH:mm').",
        "# SESSION_SECRET e DASHBOARD_PASSWORD nasceram nesta maquina: nao copie",
        "# este arquivo para outro PC.",
        "",
        "WEB_HOST=127.0.0.1",
        "WEB_PORT=$Porta",
        "DASHBOARD_PASSWORD=$Senha",
        "SESSION_SECRET=$(Segredo-Aleatorio 32)",
        "",
        "DESKTOP_MODE=true",
        "",
        "WHATSAPP_MODE=dom",
        "WHATSAPP_GROUP_NAME=$Grupo",
        "WHATSAPP_PROFILE_DIR=$Destino\perfis\whatsapp",
        "",
        "SIMULATOR_MODE=local",
        "SIM_BOT_PATH=$Destino\arqueiro",
        "SIMULATOR_PROFILE_DIR=$Destino\perfis\simulador",
        "",
        "DB_PATH=$Destino\dados\bot.db",
        "STATE_PATH=$Destino\dados\state.json",
        "COMPROVANTES_DIR=$Destino\comprovantes",
        "",
        "MASK_CPF_IN_UI=true",
        "IMAGE_SHOW_CLIENT_DATA=false",
        "TIMEZONE=America/Sao_Paulo"
    )
    Set-Content -LiteralPath $env_path -Value $linhas -Encoding UTF8
    Ok "configuracao gravada"

    $cred = Join-Path $Destino "arqueiro\credenciais.ini"
    if (-not (Test-Path $cred)) {
        Set-Content -LiteralPath $cred -Encoding UTF8 -Value @(
            "; Acesso do simulador ao portal do Santander.",
            "; Preencha aqui OU defina SANTANDER_CPF / SANTANDER_SENHA no ambiente.",
            "[santander]",
            "cpf =",
            "senha ="
        )
        Aviso "arqueiro\credenciais.ini criado em branco: preencha antes de simular"
    }
}


function Registrar-Desinstalador {
    $chave = "HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\AllanaBot"
    New-Item -Path $chave -Force | Out-Null
    $desinstalar = "powershell.exe -ExecutionPolicy Bypass -File `"$Destino\instalador\desinstalar.ps1`""
    $tamanho = 0
    try {
        $tamanho = [int](((Get-ChildItem $Destino -Recurse -File -ErrorAction SilentlyContinue |
            Measure-Object Length -Sum).Sum) / 1KB)
    } catch {}
    $versao = "0.2.0"
    $changelog = Join-Path $Destino "CHANGELOG.md"
    if (Test-Path $changelog) {
        $m = Select-String -Path $changelog -Pattern '^## \[(\d+\.\d+\.\d+)\]' | Select-Object -First 1
        if ($m) { $versao = $m.Matches[0].Groups[1].Value }
    }
    Set-ItemProperty $chave DisplayName "Allana Bot"
    Set-ItemProperty $chave DisplayVersion $versao
    Set-ItemProperty $chave Publisher "Allana"
    Set-ItemProperty $chave InstallLocation $Destino
    Set-ItemProperty $chave UninstallString $desinstalar
    Set-ItemProperty $chave QuietUninstallString "$desinstalar -Silencioso"
    Set-ItemProperty $chave EstimatedSize $tamanho
    Set-ItemProperty $chave NoModify 1
    Set-ItemProperty $chave NoRepair 1
    $icone = Join-Path $Destino "instalador\allana.ico"
    if (Test-Path $icone) { Set-ItemProperty $chave DisplayIcon $icone }
}

# ------------------------------------------------------------------ execucao
Write-Host ""
Write-Host "  Allana Bot - instalacao" -ForegroundColor White
Write-Host "  destino: $Destino"

try {
    $python = Achar-Python
    if (-not $python) {
        if ($SemNavegador) { throw "Python 3.11+ nao encontrado (e -SemNavegador pula a instalacao)" }
        $python = Instalar-Python
    }
    Ok "Python: $python"

    Copiar-Arquivos
    $venvPy = Preparar-Venv $python
    Instalar-Chromium $venvPy
    Perguntar-Configuracao
    Escrever-Env
    Registrar-Desinstalador

    Write-Host ""
    Write-Host "  Pronto." -ForegroundColor Green
    Write-Host "  Abra pelo atalho 'Allana Bot' ou rode: $Destino\iniciar.bat"
    Write-Host "  O painel responde em http://127.0.0.1:$Porta"
    if ($SemPerguntas) { Write-Host "  Senha do painel: $Senha" -ForegroundColor Yellow }
    Write-Host ""
    Write-Host "  Antes do primeiro uso:" -ForegroundColor White
    Write-Host "   1. preencha $Destino\arqueiro\credenciais.ini (acesso ao portal);"
    Write-Host "   2. na primeira execucao, leia o QR Code do WhatsApp na aba Status."
    Write-Host ""
} catch {
    Write-Host ""
    Write-Host "  A instalacao parou: $($_.Exception.Message)" -ForegroundColor Red
    Write-Host "  Nada foi deixado pela metade em $Destino? Confira e rode de novo." -ForegroundColor Red
    Read-Host "  Pressione Enter para sair..."
    exit 1
}

if (-not $SemPerguntas) {
    Write-Host "  Pressione Enter para fechar."
    Read-Host | Out-Null
}
