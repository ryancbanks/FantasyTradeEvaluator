#ifndef AppVersion
  #error AppVersion must be provided with /DAppVersion=...
#endif
#ifndef OutputDir
  #error OutputDir must be provided with /DOutputDir=...
#endif

#define AppName "Fantasy Trade Evaluator"

[Setup]
AppId={{D3BD4532-F2A7-49B6-811A-D4D30AEE3665}
AppName={#AppName} Uninstaller
AppVersion={#AppVersion}
AppPublisher=Fantasy Trade Evaluator
CreateAppDir=no
CreateUninstallRegKey=no
Uninstallable=no
PrivilegesRequired=lowest
MinVersion=10.0.22000
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir={#OutputDir}
OutputBaseFilename=FantasyTradeEvaluator-{#AppVersion}-windows-x64-Uninstall
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
DisableWelcomePage=yes
DisableProgramGroupPage=yes
DisableReadyPage=yes
DisableFinishedPage=yes
AllowCancelDuringInstall=no

[Code]
const
  AppDisplayName = 'Fantasy Trade Evaluator';
  UninstallKey = 'Software\Microsoft\Windows\CurrentVersion\Uninstall\{FD659318-22E8-45E3-A51B-1BF298CBFC90}_is1';
  LookupMissing = 0;
  LookupValid = 1;
  LookupInvalid = 2;

var
  ChildExitCode: Integer;

function IsExpectedUninstallerName(const Filename: String): Boolean;
var
  Index: Integer;
  Name: String;
begin
  Result := False;
  Name := Lowercase(ExtractFileName(Filename));
  if (Length(Name) <> 12) or (Copy(Name, 1, 5) <> 'unins') or
     (Copy(Name, 9, 4) <> '.exe') then
    Exit;
  for Index := 6 to 8 do
    if (Name[Index] < '0') or (Name[Index] > '9') then
      Exit;
  Result := True;
end;

function UnquotePath(const Value: String; var Filename: String): Boolean;
var
  Clean: String;
begin
  Result := False;
  Clean := Trim(Value);
  if (Length(Clean) < 3) or (Clean[1] <> '"') or
     (Clean[Length(Clean)] <> '"') then
    Exit;
  Filename := Copy(Clean, 2, Length(Clean) - 2);
  Result := Pos('"', Filename) = 0;
end;

function QueryRegisteredUninstaller(const RootKey: Integer;
  var Uninstaller: String): Integer;
var
  InstallLocation, RegisteredCommand, RegisteredName: String;
begin
  Result := LookupMissing;
  Uninstaller := '';
  if not RegKeyExists(RootKey, UninstallKey) then
    Exit;
  Result := LookupInvalid;
  InstallLocation := '';
  RegisteredCommand := '';
  RegisteredName := '';
  if not RegQueryStringValue(RootKey, UninstallKey, 'DisplayName', RegisteredName) or
     (CompareText(Copy(RegisteredName, 1, Length(AppDisplayName)),
       AppDisplayName) <> 0) or
     not RegQueryStringValue(RootKey, UninstallKey, 'InstallLocation', InstallLocation) or
     not RegQueryStringValue(RootKey, UninstallKey, 'UninstallString', RegisteredCommand) or
     not UnquotePath(RegisteredCommand, Uninstaller) then
    Exit;
  if not PathIsRooted(InstallLocation) or not PathIsRooted(Uninstaller) then
    Exit;
  InstallLocation := ExpandFileName(InstallLocation);
  Uninstaller := ExpandFileName(Uninstaller);
  if not IsExpectedUninstallerName(Uninstaller) or
     not PathSame(Uninstaller, PathCombine(InstallLocation, ExtractFileName(Uninstaller))) or
     not FileExists(Uninstaller) then
    Exit;
  Result := LookupValid;
end;

function FindRegisteredUninstaller(var Uninstaller: String): Integer;
begin
  Result := QueryRegisteredUninstaller(HKCU64, Uninstaller);
  if Result = LookupMissing then
    Result := QueryRegisteredUninstaller(HKCU32, Uninstaller);
end;

procedure CurStepChanged(CurStep: TSetupStep);
var
  ErrorCode: Integer;
  LookupResult: Integer;
  Parameters, Uninstaller: String;
begin
  if CurStep <> ssPostInstall then
    Exit;
  LookupResult := FindRegisteredUninstaller(Uninstaller);
  if LookupResult = LookupMissing then
  begin
    if not WizardSilent then
      MsgBox(AppDisplayName + ' is not installed for this Windows user.',
        mbInformation, MB_OK);
    Exit;
  end;
  if LookupResult = LookupInvalid then
  begin
    ChildExitCode := 1;
    if not WizardSilent then
      MsgBox(AppDisplayName + '''s uninstall registration is damaged or unsafe. ' +
        'Reinstall the app, then run this uninstaller again.', mbError, MB_OK);
    Exit;
  end;
  Parameters := '';
  if WizardSilent then
    Parameters := '/VERYSILENT /SUPPRESSMSGBOXES /NORESTART';
  if not Exec(Uninstaller, Parameters, ExtractFileDir(Uninstaller), SW_SHOWNORMAL,
    ewWaitUntilTerminated, ErrorCode) then
  begin
    ChildExitCode := 1;
    if not WizardSilent then
      MsgBox('Windows could not start the registered uninstaller: ' +
        SysErrorMessage(ErrorCode), mbError, MB_OK);
    Exit;
  end;
  ChildExitCode := ErrorCode;
  if (ChildExitCode <> 0) and not WizardSilent then
    MsgBox('The registered uninstaller returned error code ' +
      IntToStr(ChildExitCode) + '.', mbError, MB_OK);
end;

function GetCustomSetupExitCode: Integer;
begin
  Result := ChildExitCode;
end;
