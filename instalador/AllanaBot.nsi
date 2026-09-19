!include "MUI2.nsh"

Name "Allana Bot"
OutFile "dist\AllanaBot_v${VERSION}-setup.exe"
InstallDir "$LOCALAPPDATA\AllanaBot"
RequestExecutionLevel user

!define MUI_ICON "allana.ico"
!define MUI_UNICON "allana.ico"

!insertmacro MUI_PAGE_WELCOME
!insertmacro MUI_PAGE_INSTFILES
!insertmacro MUI_PAGE_FINISH

!insertmacro MUI_UNPAGE_WELCOME
!insertmacro MUI_UNPAGE_CONFIRM
!insertmacro MUI_UNPAGE_INSTFILES
!insertmacro MUI_UNPAGE_FINISH

!insertmacro MUI_LANGUAGE "PortugueseBR"

Section "Allana Bot" SecBot
  SectionIn RO
  
  InitPluginsDir
  SetOutPath "$PLUGINSDIR"
  
  ; Empacota todo o codigo preparado no PAYLOAD
  File /r "${PAYLOAD}\*.*"
  ; O bootstrap chama o instalar.ps1, que faz a copia de fato para LOCALAPPDATA
  ExecWait '"cmd.exe" /c bootstrap.cmd "-SemPerguntas"'
  
  ; Depois da instalacao, grava o uninstaller no diretorio final
  SetOutPath "$INSTDIR"
  WriteUninstaller "$INSTDIR\uninstall.exe"

  ; Cria atalhos lendo o caminho real (suporta OneDrive)
  ReadRegStr $0 HKCU "Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders" "Desktop"
  ExpandEnvStrings $0 $0
  ReadRegStr $1 HKCU "Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders" "Programs"
  ExpandEnvStrings $1 $1

  CreateShortCut "$0\Allana Bot.lnk" "$INSTDIR\iniciar.bat" "" "$INSTDIR\instalador\allana.ico"
  CreateShortCut "$1\Allana Bot.lnk" "$INSTDIR\iniciar.bat" "" "$INSTDIR\instalador\allana.ico"
SectionEnd

Section "Desinstalar"
  ExecWait 'powershell.exe -ExecutionPolicy Bypass -File "$INSTDIR\instalador\desinstalar.ps1" -Silencioso'
  ReadRegStr $0 HKCU "Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders" "Desktop"
  ExpandEnvStrings $0 $0
  ReadRegStr $1 HKCU "Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders" "Programs"
  ExpandEnvStrings $1 $1
  Delete "$0\Allana Bot.lnk"
  Delete "$1\Allana Bot.lnk"
  Delete "$INSTDIR\uninstall.exe"
  RMDir "$INSTDIR"
SectionEnd
