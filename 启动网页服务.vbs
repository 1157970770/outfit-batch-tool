' ============================================================
'  启动换装批量生产器网页服务（完全无控制台窗口）
'  双击即用：服务已在跑就直接打开浏览器，否则静默启动后再打开。
'  启动日志：web-data\logs\launcher.log
'  本文件可以复制到桌面使用（找不到 run_web.py 时会回到仓库目录）。
' ============================================================
Option Explicit

Dim fso, shell, base, python, runWeb, logFile, cmd, q, home

Set fso = CreateObject("Scripting.FileSystemObject")
Set shell = CreateObject("WScript.Shell")

base = fso.GetParentFolderName(WScript.ScriptFullName)
If Not fso.FileExists(base & "\run_web.py") Then
    base = "D:\批量视频任务设计区"
End If
runWeb = base & "\run_web.py"

python = "C:\Users\EDY\.dsh\dsh-runtimes\dsh-primary-runtime\dependencies\python\python.exe"
If Not fso.FileExists(python) Then
    home = shell.ExpandEnvironmentStrings("%USERPROFILE%")
    If fso.FileExists(home & "\.local\bin\python.exe") Then
        python = home & "\.local\bin\python.exe"
    Else
        python = "python"
    End If
End If

If PortBusy() Then
    shell.Run "http://127.0.0.1:8765/", 1, False
    WScript.Quit 0
End If

logFile = base & "\web-data\logs\launcher.log"
q = Chr(34)
cmd = "cmd /c " & q & python & q & " -u " & q & runWeb & q & " > " & q & logFile & q & " 2>&1"

shell.CurrentDirectory = base
shell.Run cmd, 0, False

WScript.Sleep 6000

If PortBusy() Then
    shell.Run "http://127.0.0.1:8765/", 1, False
Else
    MsgBox "网页服务没能启动。" & vbCrLf & vbCrLf & _
           "请查看日志：" & vbCrLf & logFile, 16, "启动失败"
End If

Function PortBusy()
    Dim execObj, text
    PortBusy = False
    On Error Resume Next
    Set execObj = shell.Exec("cmd /c netstat -ano | findstr "":8765"" | findstr LISTENING")
    text = execObj.StdOut.ReadAll()
    If Len(Trim(text)) > 0 Then PortBusy = True
    On Error GoTo 0
End Function
