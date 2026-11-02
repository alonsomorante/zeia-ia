' Autoarranque ZEIA-IA (lo registra scripts\instalar_autoarranque.cmd).
' Al iniciar sesion lanza el bot de Telegram y la webapp en vivo, ocultos.
' No duplica: si ya hay un proceso con telegram_bot.py o webapp.py no relanza
' ese servicio. Log: logs\autoarranque.log
Option Explicit

Dim sh, fso, raiz
Set sh = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
raiz = fso.GetParentFolderName(fso.GetParentFolderName(WScript.ScriptFullName))
sh.CurrentDirectory = raiz

If Not fso.FolderExists(raiz & "\logs") Then fso.CreateFolder(raiz & "\logs")

If ProcesoCorriendo("telegram_bot.py") Then
    Log "bot ya estaba corriendo (telegram_bot.py)"
Else
    sh.Run "cmd /c """ & raiz & "\scripts\telegram_bot.cmd""", 0, False
    Log "bot lanzado (scripts\telegram_bot.cmd)"
End If

' Dar tiempo a que el bot abra los tuneles antes de arrancar la webapp.
WScript.Sleep 8000

If ProcesoCorriendo("webapp.py") Then
    Log "webapp ya estaba corriendo (webapp.py)"
Else
    sh.Run "cmd /c """ & raiz & "\scripts\webapp_en_vivo.cmd""", 0, False
    Log "webapp lanzada (scripts\webapp_en_vivo.cmd)"
End If

Function ProcesoCorriendo(patron)
    Dim wmi, procesos
    Set wmi = GetObject("winmgmts:\\.\root\cimv2")
    Set procesos = wmi.ExecQuery( _
        "SELECT ProcessId FROM Win32_Process WHERE CommandLine LIKE '%" & patron & "%'")
    ProcesoCorriendo = (procesos.Count > 0)
End Function

Sub Log(texto)
    Dim f
    Set f = fso.OpenTextFile(raiz & "\logs\autoarranque.log", 8, True)
    f.WriteLine Now & "  " & texto
    f.Close
End Sub
