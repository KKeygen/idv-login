#ifndef AppName
#define AppName "IDV Login"
#endif

#ifndef AppVersion
#define AppVersion "dev"
#endif

#ifndef OutputBaseFilename
#define OutputBaseFilename "idv-login"
#endif

#ifndef OutputDir
#define OutputDir "."
#endif

#ifndef LangFile
#ifexist "ChineseSimplified.isl"
#define LangFile "ChineseSimplified.isl"
#else
#define LangFile "compiler:Languages\ChineseSimplified.isl"
#endif
#endif

[Setup]
AppName={#AppName}
AppVersion={#AppVersion}
DefaultDirName={code:GetDefaultDir}
AppendDefaultDirName=no
CreateUninstallRegKey=no
Uninstallable=no
OutputBaseFilename={#OutputBaseFilename}
PrivilegesRequired=lowest
Compression=lzma
SolidCompression=yes
OutputDir={#OutputDir}

LicenseFile=LICENSE.txt
ShowLanguageDialog=no
LanguageDetectionMethod=none
DirExistsWarning=no

[Languages]
Name: "chinesesimplified"; MessagesFile: "{#LangFile}"

[Messages]
ErrorReplacingExistingFile=尝试替换现有文件时出错，很可能是您更新前还未关闭旧版本工具。请关闭正在运行的旧版工具后点击重试。原始信息：
SelectDirLabel3=安装程序会将 [name] 放入下列文件夹。此工具为绿色版，安装工具过程不会写入注册表项目。

[Tasks]
Name: "desktopicon"; Description: "创建桌面快捷方式"; GroupDescription: "附加图标"
Name: "desktopiconmgr"; Description: "体验新版GUI，在桌面上创建GUI快捷方式"; GroupDescription: "附加图标"

[Files]
Source: "..\dist\python-embed\*"; DestDir: "{app}\python-embed"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "..\dist\src\*"; DestDir: "{app}\src"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "..\dist\assets\mpay.dll"; DestDir: "{app}\assets"; Flags: ignoreversion
Source: "..\dist\assets\mpay_default_skin.zip"; DestDir: "{app}\assets"; Flags: ignoreversion
Source: "..\dist\点我启动工具.bat"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\assets\icon.ico"; DestDir: "{app}"; Flags: ignoreversion

[InstallDelete]
Type: filesandordirs; Name: "{app}\*"
Type: files; Name: "{autodesktop}\IDV Login - 渠道服管理.lnk"
Type: files; Name: "{autodesktop}\IDV Login - 新版界面.lnk"

[Icons]
Name: "{autodesktop}\IDV Login"; Filename: "{app}\点我启动工具.bat"; Tasks: desktopicon; IconFilename: "{app}\icon.ico"; AfterInstall: MarkShortcutRunAsAdmin(ExpandConstant('{autodesktop}\IDV Login.lnk'))
Name: "{autodesktop}\IDV Login-GUI"; Filename: "{app}\点我启动工具.bat"; Parameters: "--open-ui"; Tasks: desktopiconmgr; IconFilename: "{app}\icon.ico"; AfterInstall: MarkShortcutRunAsAdmin(ExpandConstant('{autodesktop}\IDV Login-GUI.lnk'))

[Run]
Filename: "{app}\点我启动工具.bat"; Description: "{cm:LaunchProgram,{#AppName}}"; Flags: nowait postinstall skipifsilent
Filename: "https://yuque.com/keygen/kg2r5k/izpgpf4g3ecqsbf3"; Description: "查看教程"; Flags: postinstall shellexec runasoriginaluser

[Code]

function GetInstallerLogPath: string;
begin
  Result := ExpandConstant('{localappdata}\IDV-Login\installer.log');
end;

procedure WriteInstallerLog(const Msg: string);
var
  LogPath: string;
  Line: string;
begin
  LogPath := GetInstallerLogPath;
  try
    ForceDirectories(ExtractFileDir(LogPath));
    Line := GetDateTimeString('yyyy-mm-dd hh:nn:ss', '-', ':') + ' ' + Msg + #13#10;
    SaveStringToFile(LogPath, Line, True);
  except
  end;
end;

procedure CopyInstallerLogToAppDir;
var
  Src: string;
  Dst: string;
begin
  Src := GetInstallerLogPath;
  if not FileExists(Src) then
    Exit;
  try
    Dst := ExpandConstant('{app}\installer.log');
    ForceDirectories(ExtractFileDir(Dst));
    FileCopy(Src, Dst, False);
  except
  end;
end;

procedure OpenURL(const URL: string);
var
  ErrorCode: Integer;
begin
  ShellExec('open', URL, '', '', SW_SHOWNORMAL, ewNoWait, ErrorCode);
end;

function IsVCRuntimeInstalledAtRoot(RootKey: Integer; const SubKey: string): Boolean;
var
  Installed: Cardinal;
begin
  Result := RegQueryDWordValue(RootKey, SubKey, 'Installed', Installed) and (Installed = 1);
end;

function HasVCRedist14: Boolean;
begin
  if IsWin64 then
  begin
    Result :=
      IsVCRuntimeInstalledAtRoot(HKLM64, 'SOFTWARE\Microsoft\VisualStudio\14.0\VC\Runtimes\x64') or
      IsVCRuntimeInstalledAtRoot(HKLM64, 'SOFTWARE\Microsoft\VisualStudio\14.0\VC\Runtimes\x86') or
      IsVCRuntimeInstalledAtRoot(HKLM,   'SOFTWARE\Microsoft\VisualStudio\14.0\VC\Runtimes\x64') or
      IsVCRuntimeInstalledAtRoot(HKLM,   'SOFTWARE\Microsoft\VisualStudio\14.0\VC\Runtimes\x86');
  end
  else
  begin
    Result :=
      IsVCRuntimeInstalledAtRoot(HKLM, 'SOFTWARE\Microsoft\VisualStudio\14.0\VC\Runtimes\x86') or
      IsVCRuntimeInstalledAtRoot(HKLM, 'SOFTWARE\Microsoft\VisualStudio\14.0\VC\Runtimes\x64');
  end;
end;

function InitializeSetup(): Boolean;
var
  WinVer: TWindowsVersion;
  Response: Integer;
  HasVC: Boolean;
begin
  Result := True;

  WriteInstallerLog('InitializeSetup begin');
  WriteInstallerLog(Format('IsWin64=%d', [Ord(IsWin64)]));

  GetWindowsVersionEx(WinVer);
  WriteInstallerLog(Format('WindowsVersion=%d.%d Build=%d SP=%d.%d', [
    WinVer.Major, WinVer.Minor, WinVer.Build, WinVer.ServicePackMajor, WinVer.ServicePackMinor
  ]));
  if (WinVer.Major < 10) or ((WinVer.Major = 10) and (WinVer.Build < 17763)) then
  begin
    WriteInstallerLog('WindowsVersionCheck=FAIL (min 10.0.17763)');
    Response := MsgBox(
      '当前系统版本过低，最低需要 Windows 10 1809 (10.0.17763) 或更高版本。' + #13#10 +
      '点击“确定”将打开说明页面；点击“取消”将退出安装。',
      mbError, MB_OKCANCEL);
    WriteInstallerLog(Format('WindowsVersionPromptResponse=%d', [Response]));
    if Response = IDCANCEL then
    begin
      WriteInstallerLog('InitializeSetup=ABORT (user cancel on Windows version prompt)');
      Result := False;
      Exit;
    end;
    OpenURL('https://www.yuque.com/keygen/kg2r5k/sni3150i6dfykkt1#qy7EN');
  end;
  WriteInstallerLog('WindowsVersionCheck=PASS');

  HasVC := HasVCRedist14;
  WriteInstallerLog(Format('HasVCRedist14=%d', [Ord(HasVC)]));
  if not HasVC then
  begin
    Response := MsgBox(
      '未检测到 VC14 运行库（Microsoft Visual C++ 2015-2022 Redistributable）。' + #13#10 +
      '不安装可能导致软件启动后闪退。' + #13#10 +
      '点击“确定”将打开下载说明页面；点击“取消”继续安装。',
      mbInformation, MB_OKCANCEL);
    WriteInstallerLog(Format('VCRedistPromptResponse=%d', [Response]));
    if Response = IDOK then
      OpenURL('https://www.yuque.com/keygen/kg2r5k/sni3150i6dfykkt1#TXNIg');
  end;
  WriteInstallerLog('InitializeSetup end (continue install)');
end;

procedure CurStepChanged(CurStep: TSetupStep);
begin
  if CurStep = ssPostInstall then
  begin
    WriteInstallerLog('CurStepChanged=ssPostInstall (copy log to {app})');
    CopyInstallerLogToAppDir;
  end;
end;

procedure MarkShortcutRunAsAdmin(ShortcutPath: String);
var
Stream: TFileStream;
Buffer: string;
begin
if not FileExists(ShortcutPath) then exit;
Stream := TFileStream.Create(ShortcutPath, fmOpenReadWrite);
try
if Stream.Size < 21 then exit;
Stream.Seek(21, soFromBeginning);
SetLength(Buffer, 1);
Stream.ReadBuffer(Buffer, 1);
Buffer[1] := Chr(Ord(Buffer[1]) or $20);
Stream.Seek(-1, soFromCurrent);
Stream.WriteBuffer(Buffer, 1);
finally
Stream.Free;
end;
end;






function CanWriteToDir(Dir: String): Boolean;
var
  TestFile: String;
begin
  Result := False;
  try
    if not DirExists(Dir) then
      if not ForceDirectories(Dir) then
        Exit;
    TestFile := AddBackslash(Dir) + '.__write_test';
    if SaveStringToFile(TestFile, 'test', False) then
    begin
      DeleteFile(TestFile);
      Result := True;
    end;
  except
    Result := False;
  end;
end;

function GetSavedInstallDir: String;
var
  FlagPath: String;
  SavedDir: String;
  SavedDirAnsi: AnsiString;
begin
  Result := '';
  FlagPath := ExpandConstant('{commonappdata}\idv-login\install_root.flag');
  if LoadStringFromFile(FlagPath, SavedDirAnsi) then
  begin
    SavedDir := String(SavedDirAnsi);
    SavedDir := Trim(SavedDir);
    if SavedDir <> '' then
      Result := SavedDir;
  end;
end;

function GetDefaultDir(Param: String): String;
var
  DDir: String;
  CDir: String;
  FallbackDir: String;
  SavedDir: String;
begin
  SavedDir := GetSavedInstallDir;
  if (SavedDir <> '') and DirExists(SavedDir) then
  begin
    Result := SavedDir;
    Exit;
  end;
  DDir := 'D:\ProgramData\IDV-Login';
  CDir := 'C:\ProgramData\IDV-Login';
  FallbackDir := ExpandConstant('{commondocs}\IDV-Login');
  if CanWriteToDir(DDir) then
    Result := DDir
  else if CanWriteToDir(CDir) then
    Result := CDir
  else
    Result := FallbackDir;
end;


procedure InitializeWizard;
begin
  WizardForm.DiskSpaceLabel.Visible := False;
end;

function IsASCII(s: string): Boolean;
var
  i: Integer;
begin
  Result := True;
  for i := 1 to Length(s) do
  begin
    if Ord(s[i]) > 127 then
    begin
      Result := False;
      Exit;
    end;
  end;
end;

function IsRootDir(Dir: String): Boolean;
var
  DriveRoot: String;
begin
  DriveRoot := AddBackslash(ExtractFileDrive(Dir));
  Result := (CompareText(AddBackslash(Dir), DriveRoot) = 0);
end;

function DirHasContent(Dir: String): Boolean;
var
  FindRec: TFindRec;
  Path: String;
begin
  Result := False;
  Path := AddBackslash(Dir) + '*';
  if FindFirst(Path, FindRec) then
  begin
    try
      repeat
        if (FindRec.Name <> '.') and (FindRec.Name <> '..') then
        begin
          Result := True;
          Exit;
        end;
      until not FindNext(FindRec);
    finally
      FindClose(FindRec);
    end;
  end;
end;

function NextButtonClick(CurPageID: Integer): Boolean;
var
  SelectedDir: String;
  SavedDir: String;
begin
  Result := True;
  if CurPageID = wpSelectDir then
  begin
    if not IsASCII(WizardDirValue) then
    begin
      MsgBox('为了保证软件稳定运行，安装路径只能包含英文字母和数字。' + #13#10 +
             '请修改路径。', mbError, MB_OK);
      Result := False;
      Exit;
    end;

    SelectedDir := WizardDirValue;
    if IsRootDir(SelectedDir) then
    begin
      WizardForm.DirEdit.Text := AddBackslash(SelectedDir) + 'IDV-Login';
      SelectedDir := WizardForm.DirEdit.Text;
    end;

    if DirExists(SelectedDir) and DirHasContent(SelectedDir) then
    begin
      SavedDir := GetSavedInstallDir;
      if (SavedDir <> '') and (CompareText(AddBackslash(SelectedDir), AddBackslash(SavedDir)) = 0) then
      begin
        if MsgBox('检测到已安装目录。继续将删除该目录下的所有文件并进行更新，是否继续？', mbConfirmation, MB_YESNO) = IDNO then
          Result := False;
      end
      else
      begin
        if MsgBox('该目录不为空。继续将删除目录下的所有文件并进行安装，是否继续？', mbConfirmation, MB_YESNO) = IDNO then
          Result := False;
      end;
    end;
  end;
end;
