# =============================================================================
#  ALLANA - copia o seu perfil do Brave para os perfis do bot
#
#  POR QUE COPIAR EM VEZ DE APONTAR PARA O SEU PERFIL:
#  o Chromium tranca o diretorio de perfil com uma trava de instancia unica.
#  Dois navegadores no MESMO diretorio nao sobem - o segundo morre com
#  "Target page, context or browser has been closed". Como o sistema abre dois
#  (WhatsApp e simulador), cada um precisa do seu proprio diretorio.
#
#  Com copias voce ganha os dois lados: eles nascem com as suas senhas e
#  sessoes salvas, e o seu Brave do dia a dia continua livre para uso.
#
#  Rode de novo sempre que trocar a senha do Santander ou refazer o login
#  no seu Brave - as copias nao se atualizam sozinhas.
#
#  USO:  clique com o botao direito -> "Executar com o PowerShell"
#        ou:  powershell -ExecutionPolicy Bypass -File sincronizar_perfis.ps1
# =============================================================================

$ErrorActionPreference = "Stop"
$raiz = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $raiz

# Sem esta checagem, rodar o script de fora da pasta do projeto faz ele cair
# nos destinos padrao e despejar centenas de MB no diretorio errado, calado.
if (-not (Test-Path (Join-Path $raiz "app")) -or -not (Test-Path (Join-Path $raiz "main.py"))) {
    Write-Host ""
    Write-Host "  [ERRO] Este script precisa ficar NA PASTA DO PROJETO." -ForegroundColor Red
    Write-Host "         Rodando de: $raiz" -ForegroundColor Red
    Write-Host "         Esperava encontrar 'app\' e 'main.py' aqui." -ForegroundColor Red
    Write-Host ""
    Read-Host "Enter para sair"
    exit 1
}

$origem = "$env:LOCALAPPDATA\BraveSoftware\Brave-Browser\User Data"
# Le' os destinos do .env, em vez de assumir. Se SIMULATOR_PROFILE_DIR ja'
# aponta para o SEU perfil real do Brave (que e' o caso quando voce quer o
# autofill do Santander), copiar para la' seria copiar a pasta sobre ela mesma:
# esse destino e' pulado.
$origemNorm = (Resolve-Path $origem -EA SilentlyContinue).Path
$destinos = @{}
foreach ($par in @(@("WHATSAPP_PROFILE_DIR", ".whatsapp-profile", "WhatsApp Web"),
                   @("SIMULATOR_PROFILE_DIR", ".simulator-profile", "simulador (Santander)"))) {
    $chave, $padrao, $rotulo = $par
    $valor = $padrao
    if (Test-Path ".env") {
        $linha = Select-String -Path ".env" -Pattern "^$chave=(.*)$" -EA SilentlyContinue |
                 Select-Object -First 1
        if ($linha) { $valor = $linha.Matches[0].Groups[1].Value.Trim() }
    }
    $abs = if ([System.IO.Path]::IsPathRooted($valor)) { $valor } else { Join-Path $raiz $valor }
    $absNorm = (Resolve-Path $abs -EA SilentlyContinue).Path
    if ($absNorm -and $origemNorm -and ($absNorm -eq $origemNorm)) {
        Write-Host "  [--] $rotulo ja usa o seu perfil do Brave direto - nada a copiar." -ForegroundColor DarkGray
        continue
    }
    $destinos[$abs] = $rotulo
}
if ($destinos.Count -eq 0) {
    Write-Host ""
    Write-Host "  Nada a sincronizar: os dois servicos ja apontam para o seu perfil." -ForegroundColor Green
    Write-Host ""
    Read-Host "Enter para fechar"
    exit 0
}

# Pastas que nao vale a pena copiar: sao cache, o navegador refaz sozinho.
# "Code Cache" sozinho tem ~180 MB. IndexedDB e Local Storage FICAM: e' onde
# mora a sessao do WhatsApp Web.
$excluir = @("Cache", "Code Cache", "GPUCache", "DawnGraphiteCache",
             "DawnWebGPUCache", "adblock_cache", "GrShaderCache",
             "ShaderCache", "component_crx_cache", "extensions_crx_cache")

Write-Host ""
Write-Host "==========================================================" -ForegroundColor Cyan
Write-Host "   Sincronizando perfis do Brave" -ForegroundColor Cyan
Write-Host "==========================================================" -ForegroundColor Cyan
Write-Host ""

# --- 1. o Brave precisa estar fechado -------------------------------------
$brave = Get-Process brave -ErrorAction SilentlyContinue
if ($brave) {
    Write-Host "  [ERRO] O Brave esta aberto ($($brave.Count) processos)." -ForegroundColor Red
    Write-Host "         Feche o Brave por completo e rode de novo." -ForegroundColor Red
    Write-Host "         Copiar com ele aberto gera perfil corrompido." -ForegroundColor Red
    Write-Host ""
    Read-Host "Enter para sair"
    exit 1
}
Write-Host "  [OK] Brave fechado." -ForegroundColor Green

if (-not (Test-Path "$origem\Default")) {
    Write-Host "  [ERRO] Perfil do Brave nao encontrado em:" -ForegroundColor Red
    Write-Host "         $origem" -ForegroundColor Red
    Read-Host "Enter para sair"
    exit 1
}
Write-Host "  [OK] Perfil de origem encontrado." -ForegroundColor Green
Write-Host ""

# --- 2. copia para cada destino -------------------------------------------
$carimbo = Get-Date -Format "yyyyMMdd-HHmmss"

foreach ($destino in $destinos.Keys) {
    $rotulo = $destinos[$destino]
    Write-Host "  --> $destino  ($rotulo)" -ForegroundColor White

    # Guarda o que ja existia, em vez de apagar. Se algo der errado, o perfil
    # antigo (com a sessao do WhatsApp, por exemplo) continua ali do lado.
    if (Test-Path $destino) {
        $backup = "$destino.bak-$carimbo"
        Move-Item $destino $backup
        Write-Host "      perfil anterior guardado em $backup" -ForegroundColor DarkGray
    }
    New-Item -ItemType Directory -Path "$destino\Default" -Force | Out-Null

    # "Local State" guarda a chave que decifra as senhas salvas. Sem ele, o
    # Login Data copiado nao abre e o autofill nao aparece.
    Copy-Item "$origem\Local State" "$destino\Local State" -Force -ErrorAction SilentlyContinue

    $argsRobo = @("$origem\Default", "$destino\Default", "/E", "/NFL", "/NDL",
                  "/NJH", "/NJS", "/NC", "/NS", "/R:1", "/W:1", "/XJ")
    foreach ($pasta in $excluir) { $argsRobo += "/XD"; $argsRobo += "$origem\Default\$pasta" }

    robocopy @argsRobo | Out-Null
    # robocopy usa 0-7 para sucesso; 8+ e' erro de verdade.
    if ($LASTEXITCODE -ge 8) {
        Write-Host "      [ERRO] robocopy retornou $LASTEXITCODE" -ForegroundColor Red
        continue
    }

    # Travas de instancia do perfil de origem nao podem viajar junto.
    Get-ChildItem $destino -Filter "Singleton*" -Force -EA SilentlyContinue | Remove-Item -Force -EA SilentlyContinue

    $mb = [math]::Round((Get-ChildItem $destino -Recurse -File -EA SilentlyContinue |
                         Measure-Object Length -Sum).Sum / 1MB, 0)
    Write-Host "      [OK] copiado - $mb MB" -ForegroundColor Green
}

Write-Host ""
Write-Host "==========================================================" -ForegroundColor Cyan
Write-Host "  Pronto." -ForegroundColor Green
Write-Host ""
Write-Host "  Os dois navegadores do bot agora abrem com as suas senhas" -ForegroundColor Gray
Write-Host "  e sessoes salvas, cada um no seu proprio diretorio." -ForegroundColor Gray
Write-Host ""
Write-Host "  Seu Brave do dia a dia continua livre - as copias sao" -ForegroundColor Gray
Write-Host "  independentes e nao travam o seu perfil." -ForegroundColor Gray
Write-Host "==========================================================" -ForegroundColor Cyan
Write-Host ""
Read-Host "Enter para fechar"
