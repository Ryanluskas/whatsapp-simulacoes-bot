!include "MUI2.nsh"

Name "Allana Bot"
OutFile "dist\AllanaBot_v${VERSION}-setup.exe"
InstallDir "$LOCALAPPDATA\AllanaBot"
RequestExecutionLevel user

!define MUI_ICON "allana.ico"
!define MUI_UNICON "allana.ico"

!insertmacro MUI_PAGE_WELCOME
!insertmacro MUI_PAGE_DIRECTORY
!insertmacro MUI_PAGE_INSTFILES
!insertmacro MUI_PAGE_FINISH

!insertmacro MUI_UNPAGE_WELCOME
!insertmacro MUI_UNPAGE_CONFIRM
!insertmacro MUI_UNPAGE_INSTFILES
!insertmacro MUI_UNPAGE_FINISH

!insertmacro MUI_LANGUAGE "PortugueseBR"

Section "Allana Bot" SecBot
  SectionIn RO
  SetOutPath "$INSTDIR"
  
  ; Empacota todo o codigo preparado
  File /r "..\app\*.*"
  
  ; Executa a instalacao do Python
  ExecWait "cmd.exe /c bootstrap.cmd"
  
  WriteUninstaller "$INSTDIR\uninstall.exe"
  CreateShortcut "$DESKTOP\Allana Bot.lnk" "$INSTDIR\iniciar.bat" "" "$INSTDIR\instalador\allana.ico"
SectionEnd

Section "Desinstalar"
  Delete "$DESKTOP\Allana Bot.lnk"
  RMDir /r "$INSTDIR"
SectionEnd
