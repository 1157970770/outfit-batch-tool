' ============================================================
'  打开视频打标器（完全无控制台窗口）
'  先试 pythonw.exe；若被本机安全策略拦下，自动退回
'  python.exe + 隐藏窗口（同样是后台启动，不闪黑框）。
'  本文件可以复制到桌面使用（找不到脚本时会回到仓库目录）。
' ============================================================
Option Explicit

Dim fso, shell, base, pyw, py, script, cmdPyw, cmdPy, errNo, errText, q

Set fso = CreateObject("Scripting.FileSystemObject")
Set shell = CreateObject("WScript.Shell")

base = fso.GetParentFolderName(WScript.ScriptFullName)
If Not fso.FileExists(base & "\启动打标器.py") Then
    base = "D:\批量视频任务设计区"
End If
script = base & "\启动打标器.py"
pyw = base & "\.tmp\tag-venv\Scripts\pythonw.exe"
py = base & "\.tmp\tag-venv\Scripts\python.exe"

If Not fso.FileExists(py) Then
    MsgBox "找不到打标器运行环境：" & vbCrLf & py & vbCrLf & vbCrLf & _
           "请先重建 .tmp\tag-venv 虚拟环境。", 16, "启动失败"
    WScript.Quit 1
End If

q = Chr(34)
cmdPyw = q & pyw & q & " " & q & script & q
cmdPy = q & py & q & " " & q & script & q

shell.CurrentDirectory = base

Err.Clear
On Error Resume Next
If fso.FileExists(pyw) Then shell.Run cmdPyw, 0, False
If Err.Number <> 0 Then
    ' pythonw 被拦（常见 1223 / 800704C7）：改用 python.exe 并隐藏它的控制台
    Err.Clear
    shell.Run cmdPy, 0, False
End If
errNo = Err.Number
errText = Err.Description
On Error GoTo 0

If errNo <> 0 Then
    MsgBox "打标器没能启动（错误 " & errNo & "）" & vbCrLf & vbCrLf & _
           "执行的命令：" & vbCrLf & cmdPy & vbCrLf & vbCrLf & _
           "系统说明：" & errText & vbCrLf & vbCrLf & _
           "错误码 1223 / 800704C7 = 进程被本机安全软件或策略拦住。", 16, "启动失败"
End If
