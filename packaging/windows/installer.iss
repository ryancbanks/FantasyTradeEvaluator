#ifndef AppVersion
  #error AppVersion must be provided with /DAppVersion=...
#endif
#ifndef SourceDir
  #error SourceDir must be provided with /DSourceDir=...
#endif
#ifndef OutputDir
  #error OutputDir must be provided with /DOutputDir=...
#endif

#define AppName "Fantasy Trade Evaluator"
#define AppExe "FantasyTradeEvaluator.exe"

[Setup]
AppId={{FD659318-22E8-45E3-A51B-1BF298CBFC90}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher=Fantasy Trade Evaluator
DefaultDirName={localappdata}\Programs\Fantasy Trade Evaluator
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
MinVersion=10.0.22000
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir={#OutputDir}
OutputBaseFilename=FantasyTradeEvaluator-{#AppVersion}-windows-x64-Setup
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
CloseApplications=yes
UninstallDisplayName={#AppName}
UninstallDisplayIcon={app}\{#AppExe}

[Files]
Source: "{#SourceDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\{#AppName}"; Filename: "{app}\{#AppExe}"
Name: "{autoprograms}\Uninstall {#AppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExe}"; Tasks: desktopicon

[Tasks]
Name: "desktopicon"; Description: "Create a &desktop shortcut"; GroupDescription: "Additional shortcuts:"; Flags: unchecked

[Run]
Filename: "{app}\{#AppExe}"; Description: "Open {#AppName}"; Flags: nowait postinstall skipifsilent
