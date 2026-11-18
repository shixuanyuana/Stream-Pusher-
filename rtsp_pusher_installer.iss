; RTSP 推流测试工具 安装包脚本（界面版）
; 用 Inno Setup 7 编译: ISCC.exe rtsp_pusher_installer.iss

#define MyAppName "RTSP推流测试工具"
#define MyAppVersion "1.6.0"
#define MyAppExeName "RTSP推流工具.exe"

[Setup]
AppId={{8E4F2C1A-9B3D-4E5A-B7C6-RTSPPUSHTOOL}}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppVerName={#MyAppName} {#MyAppVersion}
DefaultDirName={autopf}\{#MyAppName}
DefaultGroupName={#MyAppName}
UninstallDisplayName={#MyAppName}
PrivilegesRequired=lowest
OutputDir=.
OutputBaseFilename=RTSP推流工具_{#MyAppVersion}_setup
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
DisableProgramGroupPage=yes
ArchitecturesInstallIn64BitMode=x64compatible

[Languages]
Name: "chinesesimplified"; MessagesFile: "compiler:Languages\ChineseSimplified.isl"

[Tasks]
Name: "desktopicon"; Description: "创建桌面快捷方式"; GroupDescription: "附加任务:"

[Files]
Source: "RTSP推流工具\RTSP推流工具.exe"; DestDir: "{app}"; Flags: ignoreversion
Source: "RTSP推流工具\rtsp_pusher.exe"; DestDir: "{app}"; Flags: ignoreversion
Source: "RTSP推流工具\ffmpeg\*"; DestDir: "{app}\ffmpeg"; Flags: ignoreversion recursesubdirs
Source: "RTSP推流工具\mediamtx\*"; DestDir: "{app}\mediamtx"; Flags: ignoreversion recursesubdirs
Source: "RTSP推流工具\使用说明.txt"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; WorkingDir: "{app}"
Name: "{group}\使用说明"; Filename: "{app}\使用说明.txt"
Name: "{group}\卸载{#MyAppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon; WorkingDir: "{app}"

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "立即启动 {#MyAppName}"; Flags: postinstall nowait skipifsilent

[UninstallDelete]
Type: filesandordirs; Name: "{app}\ffmpeg_video*.log"
Type: filesandordirs; Name: "{app}\selftest_result.txt"

[Messages]
FinishedLabelNoIcons=安装完成！%n在「{group}」或桌面找到「{#MyAppName}」启动，选择视频目录即可推流。%n详细用法见安装目录下的使用说明.txt。
FinishedLabel=安装完成！%n在「{group}」或桌面找到「{#MyAppName}」启动，选择视频目录即可推流。%n详细用法见安装目录下的使用说明.txt。
