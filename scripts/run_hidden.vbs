' Runs run_pipeline.bat with NO console window (window style 0) and waits for it.
' Used by the Task Scheduler task: a visible console window at 12:00 was being
' closed, which killed the run with 0xC000013A (-1073741510, STATUS_CONTROL_C_EXIT).
' The exit code of the pipeline (0 ok, 2 alarm, 1 crash) is passed through, so Task
' Scheduler's "Last Result" shows an alarm as non-zero.
Dim fso, bat, rc, i, extra
Set fso = CreateObject("Scripting.FileSystemObject")
bat = fso.BuildPath(fso.GetParentFolderName(WScript.ScriptFullName), "run_pipeline.bat")
extra = ""
For i = 0 To WScript.Arguments.Count - 1
    extra = extra & " " & WScript.Arguments(i)   ' e.g. --check-only
Next
rc = CreateObject("WScript.Shell").Run("""" & bat & """" & extra, 0, True)
WScript.Quit rc
