@echo off
rem Primeiro arquivo que o setup.exe roda: extrai o pacote e chama o instalador.
setlocal
set RAIZ=%~dp0

powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "Expand-Archive -LiteralPath '%RAIZ%payload.zip' -DestinationPath '%RAIZ%app' -Force"
if errorlevel 1 (
  echo.
  echo Nao consegui extrair o pacote. Baixe o setup de novo.
  pause
  exit /b 1
)

powershell -NoProfile -ExecutionPolicy Bypass -File "%RAIZ%app\instalador\instalar.ps1" %*
exit /b %errorlevel%
