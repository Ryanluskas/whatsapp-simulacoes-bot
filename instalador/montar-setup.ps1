<#
    Monta o AllanaBot-setup.exe.

    Roda na maquina de quem publica, nao na de quem instala. O pacote leva:

      - o codigo do repositorio (git archive: so' o que esta versionado, entao
        .env, banco, perfis e comprovantes ficam de fora por construcao);
      - o codigo do Arqueiro (bot.py e companhia), SEM dado de cliente e SEM
        credencial -- a lista de permitidos esta em $ARQUEIRO_PERMITIDO e um
        conferente recusa o pacote se algo proibido escapar.

    O executavel e' gerado com o IExpress, que ja' vem no Windows: nenhuma
    ferramenta extra precisa ser instalada.

    Uso:
      powershell -ExecutionPolicy Bypass -File instalador\montar-setup.ps1
#>
[CmdletBinding()]
param(
    [string]$Repo = "",
    [string]$Arqueiro = "$env:USERPROFILE\Downloads\arqueiro",
    [string]$Saida = "",
    [string]$Versao = "1.0.0",
    # Monta o pacote sem o Arqueiro (o instalador pergunta o caminho depois).
    [switch]$SemArqueiro
)

$ErrorActionPreference = "Stop"
# $PSScriptRoot fica vazio dentro de param(); resolve aqui.
$Aqui = $PSScriptRoot
if (-not $Aqui) { $Aqui = Split-Path -Parent $MyInvocation.MyCommand.Path }
if (-not $Repo) { $Repo = Split-Path -Parent $Aqui }
if (-not $Saida) { $Saida = Join-Path $Aqui "dist" }

# Codigo do Arqueiro. Tudo que nao estiver aqui fica de fora -- inclusive
# credenciais.ini, credentials.json, clientes.csv, bot_log.txt e as planilhas.
$ARQUEIRO_PERMITIDO = @(
    "bot.py", "gui.py", "gmail_otp.py", "otp_flow.py", "requirements.txt",
    "README.md", "iniciar.bat", "instalar.bat", "detectar_python.bat", "diagnostico.bat"
)

# Se qualquer um destes aparecer no pacote, a montagem para.
$PROIBIDOS = @(
    "credenciais.ini", "credentials.json", "token.json", ".env", "bot.db",
    "*.xlsx", "*.csv", "bot_log*.txt", "*.pem", "*.key", "*.pfx", "state.json",
    # o proprio setup da montagem anterior nao pode entrar no pacote novo
    "*.exe"
)

function Passo($t) { Write-Host "`n>> $t" -ForegroundColor Cyan }
function Ok($t) { Write-Host "   $t" -ForegroundColor Green }

$trabalho = Join-Path ([IO.Path]::GetTempPath()) ("allana-setup-" + [guid]::NewGuid().ToString("N").Substring(0, 8))
$app = Join-Path $trabalho "app"
New-Item -ItemType Directory -Force -Path $app, $Saida | Out-Null

try {
    # ---------------------------------------------------------------- codigo
    Passo "Exportando o repositorio (git archive HEAD)"
    $zipRepo = Join-Path $trabalho "repo.zip"
    & git -C $Repo archive --format=zip -o $zipRepo HEAD
    if ($LASTEXITCODE -ne 0) { throw "git archive falhou" }
    Expand-Archive -LiteralPath $zipRepo -DestinationPath $app -Force
    Remove-Item $zipRepo -Force
    Ok "$((Get-ChildItem $app -Recurse -File).Count) arquivos do repositorio"

    # A pasta instalador/ vem da copia de trabalho: e' ela que estou ajustando.
    # Criar o destino ANTES: com a pasta faltando, o Copy-Item aninha
    # instalador\instalador e o bootstrap nao acha o instalar.ps1.
    $destInst = Join-Path $app "instalador"
    New-Item -ItemType Directory -Force -Path $destInst | Out-Null
    # -Exclude nao filtra pasta com -LiteralPath: sem este Where-Object, o
    # dist\AllanaBot-setup.exe da montagem anterior entrava dentro do pacote.
    Get-ChildItem -LiteralPath $Aqui | Where-Object {
        $_.Name -ne "dist" -and $_.Extension -ne ".sed" -and $_.Extension -ne ".exe"
    } | ForEach-Object {
        Copy-Item -LiteralPath $_.FullName -Destination $destInst -Recurse -Force
    }

    # -------------------------------------------------------------- arqueiro
    if (-not $SemArqueiro) {
        Passo "Copiando o codigo do Arqueiro (sem dados e sem credenciais)"
        if (-not (Test-Path $Arqueiro)) { throw "nao achei o Arqueiro em $Arqueiro" }
        $destinoArq = Join-Path $app "arqueiro"
        New-Item -ItemType Directory -Force -Path $destinoArq | Out-Null
        $copiados = 0
        foreach ($nome in $ARQUEIRO_PERMITIDO) {
            $origem = Join-Path $Arqueiro $nome
            if (Test-Path $origem) { Copy-Item $origem $destinoArq -Force; $copiados++ }
        }
        Set-Content -LiteralPath (Join-Path $destinoArq "credenciais.ini.exemplo") -Encoding UTF8 -Value @(
            "; Copie para credenciais.ini e preencha. Este arquivo NUNCA vai no pacote.",
            "[acesso]",
            "cpf =",
            "senha ="
        )
        Ok "$copiados arquivos de codigo"
    }

    # ----------------------------------------------------------- higiene
    # CPF de exemplo em COMENTARIO vira zeros na copia -- o arquivo original
    # do Arqueiro nao e' tocado. Em linha de codigo, nao mexe: o conferente
    # abaixo recusa o pacote e a pessoa decide o que fazer.
    if (Test-Path (Join-Path $app "arqueiro")) {
        Passo "Higienizando comentarios com numero de 11 digitos"
        $limpos = 0
        foreach ($f in Get-ChildItem (Join-Path $app "arqueiro") -Recurse -File -Include *.py,*.md,*.txt,*.bat,*.ini) {
            $linhas = Get-Content -LiteralPath $f.FullName
            $mudou = $false
            for ($i = 0; $i -lt $linhas.Count; $i++) {
                $linha = $linhas[$i]
                if ($linha -notmatch '(?<!\d)\d{11}(?!\d)') { continue }
                if ($linha.TrimStart() -match '^(#|;|rem\s|::)') {
                    $linhas[$i] = [regex]::Replace($linha, '(?<!\d)\d{11}(?!\d)', '000.000.000-00')
                    $mudou = $true
                }
            }
            if ($mudou) { Set-Content -LiteralPath $f.FullName -Value $linhas -Encoding UTF8; $limpos++ }
        }
        Ok "$limpos arquivo(s) com comentario higienizado"
    }

    # ------------------------------------------------------------ conferente
    Passo "Conferindo se nao escapou segredo nem dado de cliente"
    $suspeitos = @()
    foreach ($padrao in $PROIBIDOS) {
        $suspeitos += Get-ChildItem $app -Recurse -File -Filter $padrao -ErrorAction SilentlyContinue |
            Where-Object { $_.Name -notlike "*.exemplo" -and $_.Name -ne ".env.example" }
    }
    # No Arqueiro, qualquer sequencia de 11 digitos e' CPF ate' prova em contrario.
    $arqDir = Join-Path $app "arqueiro"
    if (Test-Path $arqDir) {
        foreach ($f in Get-ChildItem $arqDir -Recurse -File) {
            if ($f.Length -gt 2MB) { continue }
            $texto = Get-Content -LiteralPath $f.FullName -Raw -ErrorAction SilentlyContinue
            if ($texto -and $texto -match '(?<!\d)\d{11}(?!\d)') { $suspeitos += $f }
        }
    }
    if ($suspeitos) {
        $suspeitos | ForEach-Object { Write-Host "   RECUSADO: $($_.FullName)" -ForegroundColor Red }
        throw "o pacote levaria arquivo proibido; ajuste a lista de permitidos"
    }
    Ok "limpo"

    # ------------------------------------------------------------- empacota
    Passo "Compactando o pacote"
    $payload = Join-Path $trabalho "payload.zip"
    # O antivirus abre os arquivos recem-copiados e o Compress-Archive falha
    # com "sendo usado por outro processo". Tentar de novo resolve.
    $tentativas = 0
    while ($true) {
        try {
            Compress-Archive -Path (Join-Path $app "*") -DestinationPath $payload -CompressionLevel Optimal -Force
            break
        } catch {
            $tentativas++
            if ($tentativas -ge 3) { throw }
            Write-Host "   arquivo ocupado; tentando de novo ($tentativas/3)" -ForegroundColor DarkGray
            Start-Sleep -Seconds 3
        }
    }
    Copy-Item (Join-Path $Aqui "bootstrap.cmd") $trabalho -Force
    $mb = [math]::Round((Get-Item $payload).Length / 1MB, 1)
    Ok "payload.zip com $mb MB"

    # ----------------------------------------------------------- setup.exe
    $makensis = "C:\Program Files (x86)\NSIS\makensis.exe"
    $exe = Join-Path (Resolve-Path $Saida).Path "AllanaBot_v$Versao-setup.exe"
    
    if (Test-Path $makensis) {
        Passo "Gerando o setup.exe com NSIS (Wizard nativo)"
        if (Test-Path $exe) { Remove-Item $exe -Force }
        
        $bootNsis = Join-Path $app "bootstrap.cmd"
        Set-Content -LiteralPath $bootNsis -Encoding ASCII -Value "@echo off`r`nsetlocal`r`nset RAIZ=%~dp0`r`npowershell -NoProfile -ExecutionPolicy Bypass -File `"%RAIZ%instalador\instalar.ps1`" %*`r`nexit /b %errorlevel%"
        
        $nsi = Join-Path $Aqui "AllanaBot.nsi"
        & $makensis "/DVERSION=$Versao" "/DPAYLOAD=$app" "/DOUTFILE=$exe" $nsi | Out-Null
        if (-not (Test-Path $exe)) { throw "o NSIS nao gerou o executavel" }
        
    } else {
        Passo "Gerando o setup.exe com IExpress (modo basico - Instale o NSIS para ter o Wizard)"
        if (Test-Path $exe) { Remove-Item $exe -Force }
        $sed = Join-Path $trabalho "AllanaBot.sed"
        Set-Content -LiteralPath $sed -Encoding ASCII -Value @(
            "[Version]",
            "Class=IEXPRESS",
            "SEDVersion=3",
            "[Options]",
            "PackagePurpose=InstallApp",
            "ShowInstallProgramWindow=0",
            "HideExtractAnimation=1",
            "UseLongFileName=1",
            "InsideCompressed=0",
            "CAB_FixedSize=0",
            "CAB_ResvCodeSigning=0",
            "RebootMode=N",
            "InstallPrompt=%InstallPrompt%",
            "DisplayLicense=%DisplayLicense%",
            "FinishMessage=%FinishMessage%",
            "TargetName=%TargetName%",
            "FriendlyName=%FriendlyName%",
            "AppLaunched=%AppLaunched%",
            "PostInstallCmd=%PostInstallCmd%",
            "AdminQuietInstCmd=%AdminQuietInstCmd%",
            "UserQuietInstCmd=%UserQuietInstCmd%",
            "SourceFiles=SourceFiles",
            "[Strings]",
            "InstallPrompt=Instalar o Allana Bot neste computador?",
            "DisplayLicense=",
            "FinishMessage=",
            "TargetName=$exe",
            "FriendlyName=Allana Bot",
            "AppLaunched=cmd /c bootstrap.cmd",
            "PostInstallCmd=<None>",
            "AdminQuietInstCmd=",
            "UserQuietInstCmd=",
            "FILE0=`"bootstrap.cmd`"",
            "FILE1=`"payload.zip`"",
            "[SourceFiles]",
            "SourceFiles0=$trabalho",
            "[SourceFiles0]",
            "%FILE0%=",
            "%FILE1%="
        )
        & "$env:WINDIR\System32\iexpress.exe" /N /Q $sed | Out-Null
        if (-not (Test-Path $exe)) { throw "o IExpress nao gerou o executavel" }
        
        # ----------------------------------------------------------- metadados (rcedit)
        Passo "Aplicando icone e metadados ao executavel"
        $rcedit = Join-Path $Aqui "rcedit.exe"
        if (-not (Test-Path $rcedit)) {
            Write-Host "   Baixando rcedit.exe (necessario para trocar o icone do IExpress)..." -ForegroundColor DarkGray
            Invoke-WebRequest -Uri "https://github.com/electron/rcedit/releases/download/v2.0.0/rcedit-x64.exe" -OutFile $rcedit
        }
        if (Test-Path $rcedit) {
            $icone = Join-Path $Aqui "allana.ico"
            & $rcedit $exe --set-icon $icone --set-version-string "FileDescription" "Allana Bot Setup" --set-version-string "ProductName" "Allana Bot" --set-file-version $Versao --set-product-version $Versao | Out-Null
            if ($LASTEXITCODE -eq 0) {
                Ok "Icone de Allana e versao $Versao injetados com sucesso"
            } else {
                Write-Host "   Aviso: rcedit.exe falhou em injetar o icone." -ForegroundColor Yellow
            }
        }
    }
    $mbExe = [math]::Round((Get-Item $exe).Length / 1MB, 1)
    Ok "$exe ($mbExe MB)"

    Write-Host ""
    Write-Host "  Pronto. Entregue este arquivo:" -ForegroundColor Green
    Write-Host "  $exe"
    Write-Host "  O PC de destino precisa de internet na instalacao (Python, dependencias e Chromium)."
    Write-Host ""
} finally {
    Remove-Item $trabalho -Recurse -Force -ErrorAction SilentlyContinue
}
