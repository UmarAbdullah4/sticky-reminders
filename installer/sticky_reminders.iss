; Inno Setup script for Sticky Reminders - turns the packaged program into a
; single Setup file with Start menu and desktop shortcuts and an uninstaller.
;
; Normally run by tools/build_installer.py, which passes the version and the
; folder PyInstaller produced. To compile by hand:
;     ISCC /DAppVersion=2.4.0 installer\sticky_reminders.iss

#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif
#ifndef SourceDir
  #define SourceDir "..\build\app\StickyReminders"
#endif
#ifndef OutputDir
  #define OutputDir "..\dist"
#endif

#define AppName "Sticky Reminders"
#define AppExe "StickyReminders.exe"
#define AppPublisher "Umar Abdullah"
#define AppURL "https://github.com/UmarAbdullah4/sticky-reminders"
; Must match APP_USER_MODEL_ID in sticky_reminders.pyw, so that a pinned
; shortcut and the running app share one taskbar button.
#define AppModelID "UmarAbdullah4.StickyReminders"

[Setup]
; Identifies the app to Windows. Never change it: it is how a newer Setup
; finds an older install and upgrades it in place.
AppId={{6F2B9C1E-3A47-4D8B-9E5C-7A1D2F4B8C63}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher={#AppPublisher}
AppPublisherURL={#AppURL}
AppSupportURL={#AppURL}/issues
AppUpdatesURL={#AppURL}/releases
VersionInfoVersion={#AppVersion}
VersionInfoProductName={#AppName}
VersionInfoDescription={#AppName} Setup

; Installs for the current user only: no administrator prompt, so it also
; works on a work PC where you cannot install for everyone.
PrivilegesRequired=lowest
DefaultDirName={autopf}\{#AppName}
DisableProgramGroupPage=yes
DisableDirPage=auto

SetupIconFile=..\assets\icon.ico
UninstallDisplayIcon={app}\{#AppExe}
UninstallDisplayName={#AppName}
OutputDir={#OutputDir}
OutputBaseFilename=StickyReminders-Setup-{#AppVersion}
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible

; The running app is closed by the [Code] section below. Setup's own
; detection could only ask the user to find it in the tray and quit it.
CloseApplications=no

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Create a &desktop shortcut"; GroupDescription: "Shortcuts:"
Name: "startup"; Description: "&Start {#AppName} when I sign in to Windows (recommended, so reminders reach you)"; GroupDescription: "Startup:"

[InstallDelete]
; An upgrade must not leave files of the previous version's bundle behind.
; Only when this app is what is already in the folder: someone may point a
; first install at a folder that holds something else.
Type: filesandordirs; Name: "{app}\_internal"; Check: IsUpgrade

[Files]
Source: "{#SourceDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "..\LICENSE"; DestDir: "{app}"; DestName: "LICENSE.txt"; Flags: ignoreversion

[Icons]
; "--show" opens the main window. Without it the app starts quietly in the
; tray, which is right at sign-in but looks like nothing happened when you
; start it yourself.
Name: "{autoprograms}\{#AppName}"; Filename: "{app}\{#AppExe}"; Parameters: "--show"; AppUserModelID: "{#AppModelID}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExe}"; Parameters: "--show"; AppUserModelID: "{#AppModelID}"; Tasks: desktopicon
; Same name and place as the app's own "Start automatically" setting uses,
; so that setting shows and controls this shortcut.
Name: "{userstartup}\{#AppName}"; Filename: "{app}\{#AppExe}"; Tasks: startup

[Run]
Filename: "{app}\{#AppExe}"; Parameters: "--show"; Description: "Start {#AppName} now"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
; The login shortcut, in case it was created later from the app's settings.
Type: files; Name: "{userstartup}\{#AppName}.lnk"

[Code]
// Notes are saved the moment they change, so stopping the app loses nothing.
// Your notes and settings live in your user profile and are left alone by
// both install and uninstall.
procedure StopRunningApp();
var
  ResultCode: Integer;
begin
  Exec(ExpandConstant('{sys}\taskkill.exe'), '/F /IM {#AppExe}', '', SW_HIDE,
       ewWaitUntilTerminated, ResultCode);
end;

function IsUpgrade(): Boolean;
begin
  Result := FileExists(ExpandConstant('{app}\{#AppExe}'));
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
begin
  StopRunningApp();
  Result := '';
end;

function InitializeUninstall(): Boolean;
begin
  StopRunningApp();
  Result := True;
end;
