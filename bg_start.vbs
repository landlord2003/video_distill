' Hidden service starter for app.py (port 8788).
' Fully windowless: pythonw.exe inside a hidden cmd -> no console window can be
' closed, so the service is immune to window-CLOSE kills (forrtl error 200).
' cmd does the log redirection, so sys.stdout/stderr are real file handles
' and print() works fine under pythonw.
Set fso = CreateObject("Scripting.FileSystemObject")
d = fso.GetParentFolderName(WScript.ScriptFullName)
CreateObject("WScript.Shell").Run _
  "cmd /c cd /d """ & d & """ && venv\Scripts\pythonw.exe app.py >> server.log 2>&1", _
  0, False
