; Inno Setup script -> dist\WAToolkit-Setup.exe   (https://jrsoftware.org/isinfo.php)
[Setup]
AppName=WA Toolkit
AppVersion=1.0.0
AppPublisher=Tilak Tiwari
DefaultDirName={autopf}\WA Toolkit
DefaultGroupName=WA Toolkit
OutputDir=..\dist
OutputBaseFilename=WAToolkit-Setup
Compression=lzma2/max
SolidCompression=yes
ArchitecturesInstallIn64BitMode=x64compatible
PrivilegesRequired=lowest
UninstallDisplayIcon={app}\WAToolkit.exe
WizardStyle=modern

[Files]
Source: "..\dist\WAToolkit\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs ignoreversion

[Icons]
Name: "{group}\WA Toolkit"; Filename: "{app}\WAToolkit.exe"
Name: "{autodesktop}\WA Toolkit"; Filename: "{app}\WAToolkit.exe"; Tasks: desktopicon

[Tasks]
Name: "desktopicon"; Description: "Create a &desktop shortcut"; GroupDescription: "Additional icons:"

[Run]
Filename: "{app}\WAToolkit.exe"; Description: "Launch WA Toolkit"; Flags: nowait postinstall skipifsilent
