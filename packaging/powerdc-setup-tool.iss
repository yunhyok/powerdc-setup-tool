#define MyAppName "SPD Manipulator for PowerDC"
#define MyAppPublisher "Yunhyok"
#define MyAppExeName "SPD Manipulator for PowerDC.exe"
#ifndef MyAppVersion
#define MyAppVersion "0.1.4"
#endif

[Setup]
AppId={{929DA9C6-4698-4C9D-8CD2-18A555E547E7}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
DefaultDirName={autopf}\{#MyAppName}
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
OutputDir=..\dist\installer
OutputBaseFilename=PowerDC-Setup-Tool-Setup-{#MyAppVersion}
Compression=lzma
SolidCompression=yes
WizardStyle=modern
; `x64` is understood by every Inno Setup 6.x. The newer architecture-identifier
; syntax that supersedes it arrived in 6.3 and is a hard compile error on
; anything older, including whatever `choco install innosetup` pins in CI.
ArchitecturesInstallIn64BitMode=x64

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[Files]
Source: "..\dist\SPD Manipulator for PowerDC\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "{cm:LaunchProgram,{#StringChange(MyAppName, '&', '&&')}}"; Flags: nowait postinstall skipifsilent
