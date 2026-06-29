; Inno Setup script — compilare con:
;   .\blackframe.ps1 build-installer
; oppure:
;   scripts\build_windows_installer.ps1

#ifndef AppVersion
  #define AppVersion "0.1.0"
#endif

#define AppName "BLACKFRAME"
#define AppPublisher "BLACKFRAME"
#define AppURL "https://github.com/nikom/smart-cam-manager"
#define AppExeName "blackframe.ps1"

[Setup]
AppId={{A8F3C2E1-9B4D-4F6A-8C1E-2D5E7F9A0B3C}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher={#AppPublisher}
AppPublisherURL={#AppURL}
AppSupportURL={#AppURL}
DefaultDirName={autopf}\{#AppName}
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
LicenseFile=
OutputDir=..\dist
OutputBaseFilename=BLACKFRAME-Setup-{#AppVersion}
Compression=lzma2/ultra64
SolidCompression=yes
WizardStyle=modern
PrivilegesRequired=admin
ArchitecturesInstallIn64BitMode=x64
SetupLogging=yes

[Languages]
Name: "italian"; MessagesFile: "compiler:Languages\Italian.isl"
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Crea collegamento sul desktop"; GroupDescription: "Collegamenti:"; Flags: unchecked
Name: "postconfig"; Description: "Configura .env e registra servizio NSSM (consigliato)"; GroupDescription: "Post-installazione:"; Flags: checked

[Dirs]
Name: "{commonappdata}\{#AppName}"; Permissions: users-modify
Name: "{commonappdata}\{#AppName}\captures"; Permissions: users-modify
Name: "{commonappdata}\{#AppName}\data"; Permissions: users-modify

[Files]
Source: "..\dist\blackframe-staging\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#AppName} — Interfaccia web"; Filename: "http://127.0.0.1:8000"; Flags: excludefromshowinnewinstall
Name: "{group}\Configura {#AppName}"; Filename: "powershell.exe"; Parameters: "-NoProfile -ExecutionPolicy Bypass -File ""{app}\deploy\post_install.ps1"""; WorkingDir: "{app}"
Name: "{group}\Log applicazione"; Filename: "{commonappdata}\{#AppName}\blackframe.log"
Name: "{autodesktop}\{#AppName}"; Filename: "http://127.0.0.1:8000"; Tasks: desktopicon

[Run]
Filename: "powershell.exe"; \
  Parameters: "-NoProfile -ExecutionPolicy Bypass -File ""{app}\deploy\post_install.ps1"" -SkipWizard -SkipService"; \
  StatusMsg: "Preparazione cartelle dati..."; \
  Flags: postinstall waituntilterminated
Filename: "powershell.exe"; \
  Parameters: "-NoProfile -ExecutionPolicy Bypass -File ""{app}\deploy\post_install.ps1"""; \
  Description: "Configura credenziali e servizio Windows"; \
  StatusMsg: "Configurazione guidata..."; \
  Flags: postinstall skipifsilent; \
  Tasks: postconfig

[UninstallRun]
Filename: "powershell.exe"; \
  Parameters: "-NoProfile -ExecutionPolicy Bypass -File ""{app}\deploy\uninstall.ps1"""; \
  Flags: runhidden waituntilterminated

[Code]
procedure CurPageChanged(CurPageID: Integer);
begin
  if CurPageID = wpFinished then
  begin
    WizardForm.FinishedLabel.Caption :=
      'BLACKFRAME è stato installato.' + #13#10 + #13#10 +
      'Interfaccia web: http://127.0.0.1:8000' + #13#10 +
      'Dati e configurazione: C:\ProgramData\BLACKFRAME' + #13#10 + #13#10 +
      'Se non hai completato la configurazione guidata, usa il collegamento ''Configura BLACKFRAME'' dal menu Start.';
  end;
end;

[Messages]
italian.WelcomeLabel2=Questo programma installerà [name/ver] sul tuo computer.%n%nÈ consigliata una connessione internet per scaricare FFmpeg (se mancante) durante la configurazione.
