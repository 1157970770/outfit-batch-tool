' ============================================================
'  以管理员身份重启网页服务（只弹 UAC 授权框，不弹命令行窗口）
'  本文件可以复制到桌面使用（找不到脚本时会回到仓库目录）。
' ============================================================
Option Explicit

Dim fso, shellApp, base, bat, args

Set fso = CreateObject("Scripting.FileSystemObject")
Set shellApp = CreateObject("Shell.Application")

base = fso.GetParentFolderName(WScript.ScriptFullName)
If Not fso.FileExists(base & "\重启网页服务-管理员.bat") Then
    base = "D:\批量视频任务设计区"
End If
bat = base & "\重启网页服务-管理员.bat"

args = "/c " & Chr(34) & Chr(34) & bat & Chr(34) & " silent" & Chr(34)
shellApp.ShellExecute "cmd.exe", args, base, "runas", 0
