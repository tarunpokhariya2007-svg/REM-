// Isolated .NET UIA comparison probe, scoped to the named disposable fixture.
using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Runtime.InteropServices;
using System.Text;
using System.Windows;
using System.Windows.Automation;

internal static class UIADirectProbe
{
    private const string Title = "REM UIA Disposable Acceptance 2026-11-24";
    private delegate bool EnumWindowsProc(IntPtr hwnd, IntPtr data);
    [DllImport("user32.dll")] private static extern bool EnumWindows(EnumWindowsProc cb, IntPtr data);
    [DllImport("user32.dll", CharSet=CharSet.Unicode)] private static extern int GetWindowText(IntPtr hwnd, StringBuilder text, int max);
    [DllImport("user32.dll")] private static extern bool IsWindowVisible(IntPtr hwnd);
    [DllImport("user32.dll")] private static extern bool ShowWindow(IntPtr hwnd, int command);
    [DllImport("user32.dll")] private static extern bool SetForegroundWindow(IntPtr hwnd);
    [DllImport("user32.dll")] private static extern bool SetWindowPos(IntPtr hwnd, IntPtr after, int x, int y, int width, int height, uint flags);
    [StructLayout(LayoutKind.Sequential)] private struct RECT { public int Left,Top,Right,Bottom; }
    [DllImport("user32.dll")] private static extern bool GetWindowRect(IntPtr hwnd, out RECT rect);
    [DllImport("user32.dll")] private static extern uint GetWindowThreadProcessId(IntPtr hwnd, out uint pid);
    static string Q(string s) { return (s ?? "").Replace("\\", "\\\\").Replace("\"", "\\\"").Replace("\r", "\\r").Replace("\n", "\\n"); }
    static string Rid(AutomationElement e) { try { return String.Join(",", Array.ConvertAll(e.GetRuntimeId(), x=>x.ToString())); } catch { return ""; } }
    static bool PatternAvailable(AutomationElement e, int id) { try { var p=AutomationPattern.LookupById(id); object current; return p!=null && e.TryGetCurrentPattern(p,out current); } catch { return false; } }
    static string Info(AutomationElement e) {
        if (e == null) return "null";
        try { var c=e.Current; var b=c.BoundingRectangle; string provider=""; try { var prop=AutomationProperty.LookupById(30107); if(prop!=null) provider=e.GetCurrentPropertyValue(prop) as string ?? ""; } catch {} return "{\"type\":\""+Q(c.ControlType.ProgrammaticName)+"\",\"name\":\""+Q(c.IsPassword?"[REDACTED]":c.Name)+"\",\"id\":\""+Q(c.AutomationId)+"\",\"class\":\""+Q(c.ClassName)+"\",\"framework\":\""+Q(c.FrameworkId)+"\",\"provider\":\""+Q(provider)+"\",\"hwnd\":\"0x"+c.NativeWindowHandle.ToString("x")+"\",\"pid\":"+c.ProcessId+",\"rid\":\""+Q(Rid(e))+"\",\"bounds\":\""+b.Left+","+b.Top+","+b.Width+","+b.Height+"\",\"offscreen\":"+(c.IsOffscreen?"true":"false")+",\"value\":"+(PatternAvailable(e,10002)?"true":"false")+",\"invoke\":"+(PatternAvailable(e,10000)?"true":"false")+",\"legacy\":"+(PatternAvailable(e,10018)?"true":"false")+"}"; } catch(Exception x) { return "{\"error\":\""+x.GetType().Name+"\"}"; }
    }
    static AutomationElement FindByType(AutomationElement root, ControlType type) {
        var cond = new PropertyCondition(AutomationElement.ControlTypeProperty,type);
        var all=root.FindAll(TreeScope.Descendants,cond);
        for(int i=0;i<all.Count;i++) if(all[i].Current.Name==(type==ControlType.Edit?"Search field":"Run test search")) return all[i];
        return null;
    }
    static int Main() {
        var hits=new List<Tuple<IntPtr,uint>>();
        EnumWindows((h,_)=>{ if(!IsWindowVisible(h))return true; var s=new StringBuilder(512); GetWindowText(h,s,512); if(s.ToString()==Title && h.ToInt64()==7276274){uint p;GetWindowThreadProcessId(h,out p);hits.Add(Tuple.Create(h,p));}return true;},IntPtr.Zero);
        if(hits.Count!=1){Console.WriteLine("{\"ok\":false,\"lookup_count\":"+hits.Count+"}");return 2;}
        IntPtr hwnd=hits[0].Item1; uint pid=hits[0].Item2;
        string stage="root_handle";
        RECT windowRect; GetWindowRect(hwnd,out windowRect);
        try {
            var byHandle=AutomationElement.FromHandle(hwnd);
            stage="desktop_root";
            var desktop=AutomationElement.RootElement;
            var byDesktop=desktop.FindFirst(TreeScope.Children,new AndCondition(
                new PropertyCondition(AutomationElement.NameProperty,Title),
                new PropertyCondition(AutomationElement.ProcessIdProperty,(int)pid)));
            var rootRows=new List<string>();
            foreach(var pair in new[]{Tuple.Create("handle",byHandle),Tuple.Create("desktop",byDesktop)}) {
                if(pair.Item2==null){rootRows.Add("{\"route\":\""+pair.Item1+"\",\"root\":null}");continue;}
                var children=pair.Item2.FindAll(TreeScope.Children,Condition.TrueCondition);
                rootRows.Add("{\"route\":\""+pair.Item1+"\",\"root\":"+Info(pair.Item2)+",\"child_count\":"+children.Count+"}");
            }
            stage="tree_views";
            var views=new List<string>();
            foreach(var v in new[]{Tuple.Create("control",TreeWalker.ControlViewWalker),Tuple.Create("raw",TreeWalker.RawViewWalker),Tuple.Create("content",TreeWalker.ContentViewWalker)}) {
                int n=0; var names=new List<string>(); var cur=v.Item2.GetFirstChild(byHandle);
                while(cur!=null && n<1000){n++;names.Add(Info(cur));cur=v.Item2.GetNextSibling(cur);}
                views.Add("{\"view\":\""+v.Item1+"\",\"children\":["+String.Join(",",names.ToArray())+"]}");
            }
            stage="from_point";
            var pointRows=new List<string>();
            var descendants=byHandle.FindAll(TreeScope.Descendants,Condition.TrueCondition);
            for(int i=0;i<descendants.Count;i++) { var el=descendants[i]; string cls=el.Current.ClassName; if(cls.IndexOf("EDIT",StringComparison.OrdinalIgnoreCase)>=0 || cls.IndexOf("BUTTON",StringComparison.OrdinalIgnoreCase)>=0 || cls.IndexOf("LISTBOX",StringComparison.OrdinalIgnoreCase)>=0) {var r=el.Current.BoundingRectangle; var p=new Point(r.Left+r.Width/2,r.Top+r.Height/2); AutomationElement at=null; string pointError=""; try { at=AutomationElement.FromPoint(p); } catch(Exception pe) { pointError=pe.GetType().Name+":"+pe.Message; } AutomationElement childHandle=null; try { if(el.Current.NativeWindowHandle!=0) childHandle=AutomationElement.FromHandle(new IntPtr(el.Current.NativeWindowHandle)); } catch { } pointRows.Add("{\"source\":"+Info(el)+",\"from_child_handle\":"+Info(childHandle)+",\"point\":\""+p.X+","+p.Y+"\",\"at_point\":"+Info(at)+",\"point_error\":\""+Q(pointError)+"\"}");}}
            Console.WriteLine("{\"ok\":true,\"pid\":"+pid+",\"hwnd\":\"0x"+hwnd.ToInt64().ToString("x")+"\",\"window_rect\":\""+windowRect.Left+","+windowRect.Top+","+windowRect.Right+","+windowRect.Bottom+"\",\"roots\":["+String.Join(",",rootRows.ToArray())+"],\"views\":["+String.Join(",",views.ToArray())+"],\"from_point\":["+String.Join(",",pointRows.ToArray())+"]}");
            return 0;
        } catch(Exception e){Console.WriteLine("{\"ok\":false,\"stage\":\""+stage+"\",\"error\":\""+e.GetType().Name+"\",\"message\":\""+Q(e.Message)+"\",\"hresult\":\"0x"+e.HResult.ToString("x8")+"\"}");return 5;}
    }
}
