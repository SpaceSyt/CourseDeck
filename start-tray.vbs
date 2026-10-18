Option Explicit

Dim files, shell, folder, python, command
Set files = CreateObject("Scripting.FileSystemObject")
Set shell = CreateObject("WScript.Shell")
folder = files.GetParentFolderName(WScript.ScriptFullName)
python = files.BuildPath(folder, ".venv\Scripts\pythonw.exe")

If Not files.FileExists(python) Then
    MsgBox "Run setup.cmd first.", vbExclamation, "CourseDeck"
    WScript.Quit 1
End If
If Not files.FileExists(files.BuildPath(folder, "frontend\dist\index.html")) Then
    MsgBox "Build the frontend first with setup.cmd.", vbExclamation, "CourseDeck"
    WScript.Quit 1
End If

shell.CurrentDirectory = folder
command = Chr(34) & python & Chr(34) & " -m coursedeck.tray"
On Error Resume Next
shell.Run command, 0, False
If Err.Number <> 0 Then
    MsgBox "Could not start CourseDeck. Run start.cmd to check the installation.", vbExclamation, "CourseDeck"
    WScript.Quit 1
End If
