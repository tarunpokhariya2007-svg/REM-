// Disposable WinForms application used only for local UI Automation acceptance.
using System;
using System.Drawing;
using System.IO;
using System.Reflection;
using System.Windows.Forms;

internal static class NativeUiaFixture
{
    private static string SwitchState(string key)
    {
        var appContext = typeof(object).Assembly.GetType("System.AppContext");
        var method = appContext == null ? null : appContext.GetMethod("TryGetSwitch",
            BindingFlags.Public | BindingFlags.Static);
        if (method == null) return "unavailable";
        var args = new object[] { key, false };
        var known = (bool)method.Invoke(null, args);
        return known ? args[1].ToString() : "unset";
    }

    [STAThread]
    private static void Main()
    {
        // Record only runtime/version switch status for this disposable
        // accessibility fixture. No UI values or user data are written.
        var target = AppDomain.CurrentDomain.SetupInformation.TargetFrameworkName ?? "unset";
        File.WriteAllText(Path.Combine(AppDomain.CurrentDomain.BaseDirectory,
            "NativeUiaFixture.runtime.txt"),
            "target=" + target +
            "\nframework=" + Environment.Version +
            "\nlegacy47=" + SwitchState("Switch.UseLegacyAccessibilityFeatures") +
            "\nlegacy472=" + SwitchState("Switch.UseLegacyAccessibilityFeatures.2") +
            "\nlegacy48=" + SwitchState("Switch.UseLegacyAccessibilityFeatures.3"));

        Application.EnableVisualStyles();
        Application.SetCompatibleTextRenderingDefault(false);
        var form = new Form {
            Text = "REM UIA Disposable Acceptance 2026-11-24",
            Name = "RemUiaAcceptanceWindow",
            StartPosition = FormStartPosition.CenterScreen,
            Size = new Size(760, 620), TopMost = true
        };
        var label = new Label { Text = "Search field", Name = "SearchLabel",
            Location = new Point(24, 24), Size = new Size(120, 24) };
        var search = new TextBox { Name = "SearchBox", AccessibleName = "Search field",
            AccessibleDescription = "Disposable UIA acceptance search field",
            Location = new Point(24, 52), Size = new Size(360, 28) };
        var result = new Label { Name = "ResultLabel", AccessibleName = "Search result status",
            Text = "No search run", Location = new Point(24, 92), Size = new Size(520, 24) };
        var list = new ListBox { Name = "ResultList", AccessibleName = "Test results",
            Location = new Point(24, 140), Size = new Size(240, 110) };
        list.Items.AddRange(new object[] { "Alpha result", "Bravo result", "Charlie result" });
        var button = new Button { Name = "SearchButton", AccessibleName = "Run test search",
            Text = "Run test search", Location = new Point(400, 50), Size = new Size(150, 32) };
        button.Click += (s, e) => {
            result.Text = "Search completed: " + search.Text;
            result.AccessibleName = result.Text;
            list.BeginUpdate();
            list.Items.Clear();
            list.Items.Add("Result for " + search.Text);
            list.EndUpdate();
        };
        var check = new CheckBox { Name = "TestCheckBox", AccessibleName = "Enable test option",
            Text = "Enable test option", AutoSize = true, Location = new Point(300, 145) };
        var combo = new ComboBox { Name = "TestComboBox", AccessibleName = "Test choices",
            DropDownStyle = ComboBoxStyle.DropDownList, Location = new Point(300, 185),
            Size = new Size(180, 30) };
        combo.Items.AddRange(new object[] { "Choice One", "Choice Two" }); combo.SelectedIndex = 0;
        var tabs = new TabControl { Name = "TestTabs", AccessibleName = "Test tabs",
            Location = new Point(24, 280), Size = new Size(500, 220) };
        tabs.TabPages.Add(new TabPage("Overview") { Name = "OverviewTab" });
        tabs.TabPages.Add(new TabPage("Details") { Name = "DetailsTab" });
        var multiline = new RichTextBox { Name = "MultilineKeyboardBox",
            AccessibleName = "Multiline keyboard field", Multiline = true,
            AcceptsTab = true, WordWrap = true,
            ScrollBars = RichTextBoxScrollBars.Vertical,
            Location = new Point(24, 515), Size = new Size(520, 76) };
        var menu = new MenuStrip { Name = "TestMenu" };
        var item = new ToolStripMenuItem("Fixture") { Name = "FixtureMenuItem" };
        menu.Items.Add(item); form.MainMenuStrip = menu;
        form.Controls.AddRange(new Control[] { label, search, result, button, list, check,
            combo, tabs, multiline, menu });
        form.Shown += (s, e) => { form.Activate(); form.BringToFront(); };
        Application.Run(form);
    }
}
