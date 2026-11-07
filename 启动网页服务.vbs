' ============================================================
'  启动换装批量生产器网页服务（完全无控制台窗口）
'  双击即用：服务已在跑就直接打开浏览器，否则静默启动后再打开。
'  端口与数据目录自动读取本文件夹 server\web.json：
'    运行区 8765 / web-data（显示「生产版」）
'    设计区 8766 / web-data-dev（web.json 里 dev_mode=true，显示「开发版」）
'  本文件是 ANSI(GBK) 编码：中文必须仍然用 GBK 保存，否则弹窗会乱码。
'  启动日志：<数据目录>\logs\launcher.log
'  本文件可以复制到桌面使用（找不到 run_web.py 时回到运行区目录）。
' ============================================================
Option Explicit

Dim fso, shell, base, python, runWeb, logFile, cmd, q, home, port, dataRoot, webJson, zone
Dim ts, txt, v

Set fso = CreateObject("Scripting.FileSystemObject")
Set shell = CreateObject("WScript.Shell")

base = fso.GetParentFolderName(WScript.ScriptFullName)
If Not fso.FileExists(base & "\run_web.py") Then
    base = "D:\批量视频任务运行区"
End If
runWeb = base & "\run_web.py"

' ---- 端口、数据目录、分区标签：读本文件夹 server\web.json，缺省 8765 / 生产版 ----
port = "8765"
dataRoot = "web-data"
zone = "生产版"
txt = ""
webJson = base & "\server\web.json"
If fso.FileExists(webJson) Then
    Set ts = fso.OpenTextFile(webJson, 1)
    txt = ts.ReadAll()
    ts.Close
    v = JsonValue(txt, "port")
    If Len(v) > 0 Then port = v
    v = JsonValue(txt, "data_root")
    If Len(v) > 0 Then dataRoot = v
    If JsonTrue(txt, "dev_mode") Then zone = "开发版"
End If

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
    ' 已经在跑：直接开浏览器，不重复启动第二个实例
    shell.Run "http://127.0.0.1:" & port & "/", 1, False
    WScript.Quit 0
End If

logFile = base & "\" & dataRoot & "\logs\launcher.log"
EnsureDir fso.GetParentFolderName(logFile)
q = Chr(34)
cmd = "cmd /c " & q & python & q & " -u " & q & runWeb & q & " > " & q & logFile & q & " 2>&1"

shell.CurrentDirectory = base
shell.Run cmd, 0, False

WScript.Sleep 6000

If PortBusy() Then
    shell.Run "http://127.0.0.1:" & port & "/", 1, False
Else
    MsgBox "网页服务没能启动。" & vbCrLf & vbCrLf & _
           "[" & zone & "]  端口 " & port & "   数据 " & dataRoot & vbCrLf & vbCrLf & _
           "请查看日志：" & vbCrLf & logFile, 16, zone & " · 启动失败"
End If

' ---- 从 web.json 文本取 "key": 值（字符串去引号，数字原样）----
Function JsonValue(txt, key)
    Dim k, p, ch, res
    JsonValue = ""
    k = Chr(34) & key & Chr(34) & ":"
    p = InStr(txt, k)
    If p = 0 Then Exit Function
    p = p + Len(k)
    Do While p <= Len(txt)
        ch = Mid(txt, p, 1)
        If ch <> " " And ch <> vbTab Then Exit Do
        p = p + 1
    Loop
    If p > Len(txt) Then Exit Function
    If Mid(txt, p, 1) = Chr(34) Then
        p = p + 1
        res = ""
        Do While p <= Len(txt)
            ch = Mid(txt, p, 1)
            If ch = Chr(34) Then Exit Do
            res = res & ch
            p = p + 1
        Loop
        JsonValue = res
    Else
        res = ""
        Do While p <= Len(txt)
            ch = Mid(txt, p, 1)
            If ch < "0" Or ch > "9" Then Exit Do
            res = res & ch
            p = p + 1
        Loop
        JsonValue = res
    End If
End Function

' ---- 取 web.json 里某个布尔键（true/false），键不存在时返回 False ----
Function JsonTrue(txt, key)
    Dim k, p, rest
    JsonTrue = False
    k = Chr(34) & key & Chr(34) & ":"
    p = InStr(txt, k)
    If p = 0 Then Exit Function
    p = p + Len(k)
    Do While p <= Len(txt)
        If Mid(txt, p, 1) <> " " And Mid(txt, p, 1) <> vbTab Then Exit Do
        p = p + 1
    Loop
    rest = LCase(Mid(txt, p, 4))
    If rest = "true" Then JsonTrue = True
End Function

' ---- 递归创建目录（cmd 重定向要求日志目录先存在）----
Sub EnsureDir(path)
    Dim parent
    If path = "" Then Exit Sub
    If fso.FolderExists(path) Then Exit Sub
    parent = fso.GetParentFolderName(path)
    If parent <> "" And Not fso.FolderExists(parent) Then EnsureDir parent
    On Error Resume Next
    fso.CreateFolder path
    On Error GoTo 0
End Sub

' ---- 端口是否已监听 ----
Function PortBusy()
    Dim execObj, text, nc
    PortBusy = False
    On Error Resume Next
    nc = "cmd /c netstat -ano | findstr " & Chr(34) & ":" & port & Chr(34) & " | findstr LISTENING"
    Set execObj = shell.Exec(nc)
    text = execObj.StdOut.ReadAll()
    If Len(Trim(text)) > 0 Then PortBusy = True
    On Error GoTo 0
End Function