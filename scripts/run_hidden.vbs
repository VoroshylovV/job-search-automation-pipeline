' Runs run_pipeline.bat with NO console window (window style 0) and waits for it.
' Used by the Task Scheduler task: a visible console window at 12:00 was being
' closed, which killed the run with 0xC000013A (-1073741510, STATUS_CONTROL_C_EXIT).
Dim fso, bat
Set fso = CreateObject("Scripting.FileSystemObject")
bat = fso.BuildPath(fso.GetParentFolderName(WScript.ScriptFullName), "run_pipeline.bat")
CreateObject("WScript.Shell").Run """" & bat & """", 0, True
