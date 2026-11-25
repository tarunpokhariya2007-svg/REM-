// One-shot launcher so the disposable Python UIA probe can be started from
// the interactive desktop app inventory without changing its security token.
using System;
using System.Diagnostics;

internal static class RunUiaProbe
{
    private static int Main()
    {
        string directory = AppDomain.CurrentDomain.BaseDirectory;
        string python = @"C:\important\Mark-LVII-main\.venv\Scripts\pythonw.exe";
        string script = System.IO.Path.Combine(directory,
            "run_uia_accessibility_probe.pyw");
        var start = new ProcessStartInfo {
            FileName = python,
            Arguments = "\"" + script + "\"",
            WorkingDirectory = directory,
            UseShellExecute = false,
            CreateNoWindow = true,
            WindowStyle = ProcessWindowStyle.Hidden
        };
        using (Process child = Process.Start(start))
        {
            child.WaitForExit(30000);
            return child.HasExited ? child.ExitCode : 3;
        }
    }
}
