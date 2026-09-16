' Removes the Startup entry and stops the companion process. Never touches the saved
' key or chat history (see alfred_launcher.py stop()).
Set fso = CreateObject("Scripting.FileSystemObject")
baseDir = fso.GetParentFolderName(WScript.ScriptFullName)
Set shell = CreateObject("WScript.Shell")
shell.Run """" & baseDir & "\pythonw.exe"" """ & baseDir & "\alfred_launcher.py"" --stop", 0, False
