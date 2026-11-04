' ============================================================
'  以管理员身份重启网页服务（只弹 UAC 授权框，不弹命令行窗口）
'  重启完成后会弹出提示，告诉你服务是否已经就绪。
'  本文件可以复制到桌面使用（找不到脚本时会回到仓库目录）。
' ============================================================
Option Explicit

Dim fso, shellApp, sh, base, bat, args, stamp, waited, done, result, up, msg, title

Set fso = CreateObject("Scripting.FileSystemObject")
Set shellApp = CreateObject("Shell.Application")
Set sh = CreateObject("WScript.Shell")

base = fso.GetParentFolderName(WScript.ScriptFullName)
If Not fso.FileExists(base & "\启动器\重启网页服务-管理员.bat") Then
    base = "D:\批量视频任务设计区"
End If
bat = base & "\启动器\重启网页服务-管理员.bat"

title = "网页服务重启"

' 先清掉上一次留下的完成标记，避免读到旧结果
stamp = sh.ExpandEnvironmentStrings("%TEMP%") & "\outfit_web_restart.stamp"
If fso.FileExists(stamp) Then fso.DeleteFile stamp, True

args = "/c " & Chr(34) & Chr(34) & bat & Chr(34) & " silent" & Chr(34)
On Error Resume Next
shellApp.ShellExecute "cmd.exe", args, base, "runas", 0
If Err.Number <> 0 Then
    ' 用户在 UAC 弹窗上点了“否”，或系统策略不允许提权
    Err.Clear
    On Error GoTo 0
    sh.Popup "已取消管理员授权，服务没有重启。" & vbCrLf & vbCrLf & _
             "要让重启生效，需要点 UAC 弹窗上的“是”。", 20, title, 48
    WScript.Quit
End If
On Error GoTo 0

' 等批处理写出完成标记（最多 90 秒；批处理自己会先等 15 秒再确认端口）
waited = 0
done = False
Do While waited < 180
    WScript.Sleep 500
    waited = waited + 1
    If fso.FileExists(stamp) Then
        done = True
        Exit Do
    End If
Loop

result = ""
If done Then
    Dim stream
    Set stream = fso.OpenTextFile(stamp, 1, False)
    result = Trim(stream.ReadLine)
    stream.Close
    fso.DeleteFile stamp, True
End If

up = ServiceUp()

If Left(result, 2) = "OK" Then
    msg = "网页服务已重启完成。" & vbCrLf & vbCrLf & _
          "访问地址：http://192.168.10.179:8765" & vbCrLf & _
          "服务进程 PID：" & Trim(Mid(result, 3)) & vbCrLf & vbCrLf & _
          "如果页面还是旧样式，请按 Ctrl+F5 强制刷新。"
    sh.Popup msg, 15, title, 64
ElseIf result = "FAIL" Then
    msg = "重启没有成功：服务没有起来（端口 8765 无监听）。" & vbCrLf & vbCrLf & _
          "请双击「启动器\重启网页服务-管理员.bat」，" & vbCrLf & _
          "在弹出的窗口里看是哪一步出错。"
    sh.Popup msg, 30, title, 48
ElseIf up Then
    msg = "网页服务已能访问，但没有读到完成标记（可能是手动启动的）。" & vbCrLf & vbCrLf & _
          "访问地址：http://192.168.10.179:8765"
    sh.Popup msg, 15, title, 64
Else
    msg = "已发起重启，但 90 秒内没有等到结果。" & vbCrLf & vbCrLf & _
          "可能是 UAC 授权框被取消了，或服务启动较慢。" & vbCrLf & _
          "请稍后刷新页面确认，仍未恢复就再运行一次。"
    sh.Popup msg, 30, title, 48
End If

' 检查 127.0.0.1:8765 是否已经能正常响应（确认“重启好了”）
Function ServiceUp()
    Dim req
    ServiceUp = False
    On Error Resume Next
    Set req = CreateObject("MSXML2.ServerXMLHTTP.6.0")
    req.setTimeouts 2000, 2000, 2000, 3000
    req.open "GET", "http://127.0.0.1:8765/", False
    req.send
    If Err.Number = 0 Then
        If req.status = 200 Then ServiceUp = True
    End If
    Err.Clear
    On Error GoTo 0
End Function
