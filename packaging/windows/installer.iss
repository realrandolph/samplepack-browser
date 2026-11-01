; Inno Setup script for the Samplepack Browser Windows installer.
;
; Build requirements:
;   1. PyInstaller output at build\windows\dist\samplepack-browser\ (see
;      .github/workflows/release.yml, job "package-windows")
;   2. icon.ico in build\windows\dist\samplepack-browser\ so the Start Menu and
;      desktop shortcuts can reference {app}\icon.ico
;
; Compile with:
;   "C:\Program Files (x86)\Inno Setup 6\ISCC.exe" packaging\windows\installer.iss
; The installer is written to dist\samplepack-browser-windows-setup.exe.

#define MyAppName "Samplepack Browser"
#define MyAppVersion "1.0.0"
#define MyAppPublisher "Samplepack Browser Team"
#define MyAppExeName "samplepack-browser.exe"

[Setup]
; {{ escapes a literal opening brace, so the stored AppId is
; {B42E7591-A8D9-4B52-87D9-C13AE4E41091}. Keep it stable: it identifies the
; application for upgrades and uninstall entries.
AppId={{B42E7591-A8D9-4B52-87D9-C13AE4E41091}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppVerName={#MyAppName} {#MyAppVersion}
AppPublisher={#MyAppPublisher}
DefaultDirName={autopf}\Samplepack Browser
DefaultGroupName=Samplepack Browser
DisableProgramGroupPage=yes
OutputDir=..\..\dist
OutputBaseFilename=samplepack-browser-windows-setup
SetupIconFile=icon.ico
UninstallDisplayIcon={app}\{#MyAppExeName}
Compression=lzma2/ultra64
SolidCompression=yes
WizardStyle=modern
; 64-bit only application: install into the native 64-bit Program Files and
; refuse installation on 32-bit Windows. x64compatible also runs under
; emulation on ARM64 Windows 11.
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
; Installing into Program Files requires elevation, matching {autopf}.
PrivilegesRequired=admin

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"

[Files]
Source: "..\..\build\windows\dist\samplepack-browser\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\Samplepack Browser"; Filename: "{app}\{#MyAppExeName}"; IconFilename: "{app}\icon.ico"
Name: "{autodesktop}\Samplepack Browser"; Filename: "{app}\{#MyAppExeName}"; IconFilename: "{app}\icon.ico"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "{cm:LaunchProgram,Samplepack Browser}"; Flags: nowait postinstall skipifsilent
