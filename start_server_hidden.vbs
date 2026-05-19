Set WshShell = CreateObject("WScript.Shell")
WshShell.CurrentDirectory = "C:\Users\Administrator\Desktop\Automation"
WshShell.Run """C:\Users\Administrator\AppData\Local\Programs\Python\Python310\pythonw.exe"" ""C:\Users\Administrator\Desktop\Automation\server.py""", 0, False
