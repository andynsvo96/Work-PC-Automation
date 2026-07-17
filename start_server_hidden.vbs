Set WshShell = CreateObject("WScript.Shell")
Set FileSystem = CreateObject("Scripting.FileSystemObject")

ScriptDirectory = FileSystem.GetParentFolderName(WScript.ScriptFullName)
VirtualPython = FileSystem.BuildPath(ScriptDirectory, ".venv\Scripts\pythonw.exe")
If FileSystem.FileExists(VirtualPython) Then
    PythonExecutable = VirtualPython
Else
    PythonExecutable = "pythonw.exe"
End If

SafeSyncScript = FileSystem.BuildPath(ScriptDirectory, "safe_sync.py")
WshShell.CurrentDirectory = ScriptDirectory
WshShell.Run Chr(34) & PythonExecutable & Chr(34) & " " & Chr(34) & SafeSyncScript & Chr(34) & " start", 0, False
