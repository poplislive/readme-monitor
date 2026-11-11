; ReadmeMonitor NSIS hooks (Tauri bundler). The scheduled check is registered by the application
; itself (per user, Task Scheduler) and points at $INSTDIR\engine\rm-engine-bg.exe. An uninstall
; removes it; an update keeps it, because the update replaces the engine at the same path.
; The runtime folder (settings, history, spending ledger) and the stored credentials are kept:
; see docs/desktop.md for deliberate removal.

; The per-user default, %LOCALAPPDATA%\ReadmeMonitor, was the runtime folder of ReadmeMonitor before
; 0.3.0 and can still hold a configuration and a spending ledger: program files never go there.
; The template has already created the folder (SetOutPath); RMDir removes it again only if empty.
!macro NSIS_HOOK_PREINSTALL
  ${If} $INSTDIR == "$LOCALAPPDATA\${PRODUCTNAME}"
    RMDir "$INSTDIR"
    StrCpy $INSTDIR "$LOCALAPPDATA\Programs\${PRODUCTNAME}"
    SetOutPath $INSTDIR
  ${EndIf}
!macroend

!macro NSIS_HOOK_PREUNINSTALL
  ${If} $UpdateMode <> 1
    DetailPrint "Removing the ReadmeMonitor scheduled check"
    nsExec::Exec '"$INSTDIR\engine\rm-engine.exe" uninstall-task'
    Pop $0
  ${EndIf}
!macroend
