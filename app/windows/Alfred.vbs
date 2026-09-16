' Double-click entry point. Runs the launcher hidden (no console flash) with the
' embeddable pythonw.exe that ships next to this file. Waits for it (True) so a timeout
' (exit code 2 — the companion's page never came up within 60s) can pop a MsgBox instead
' of failing silently; a normal run (exit 0) opens the browser itself and returns fast.
Set fso = CreateObject("Scripting.FileSystemObject")
baseDir = fso.GetParentFolderName(WScript.ScriptFullName)
Set shell = CreateObject("WScript.Shell")
rc = shell.Run("""" & baseDir & "\pythonw.exe"" """ & baseDir & "\alfred_launcher.py""", 0, True)
If rc <> 0 Then
    MsgBox "Alfred's companion did not start. See %APPDATA%\Alfred\launcher.log for details.", vbExclamation, "Alfred"
End If
