import contextvars
import unittest
import ast
from pathlib import Path
from types import SimpleNamespace

import main
from core.tool_security import TOOL_SECURITY, tool_policy, unreviewed_tool_names

ROOT = Path(__file__).resolve().parents[1]


class ToolSecurityManifestTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.inline = {row["name"] for row in main.TOOL_DECLARATIONS}
        cls.actions = cls._declaration_names(ROOT / "actions", "TOOL")
        cls.plugins = cls._declaration_names(ROOT / "plugins", "PLUGIN")
        cls.active = cls.inline | cls.actions | cls.plugins

    @staticmethod
    def _declaration_names(folder, variable):
        """Read declaration identifiers from syntax without importing plugins."""
        names = set()
        for path in folder.glob("*.py"):
            if path.name.startswith("_"):
                continue
            try:
                tree = ast.parse(path.read_text(encoding="utf-8"))
            except (OSError, SyntaxError):
                continue
            for node in tree.body:
                if not isinstance(node, ast.Assign):
                    continue
                if not any(isinstance(target, ast.Name) and target.id == variable
                           for target in node.targets):
                    continue
                value = node.value
                if variable == "TOOL" and isinstance(value, ast.Dict):
                    for key, item in zip(value.keys, value.values):
                        if isinstance(key, ast.Constant) and key.value == "name":
                            if isinstance(item, ast.Constant) and isinstance(item.value, str):
                                names.add(item.value)
                elif variable == "PLUGIN":
                    if isinstance(value, ast.Call) and value.args:
                        first = value.args[0]
                        if isinstance(first, ast.Constant) and isinstance(first.value, str):
                            names.add(first.value)
                    elif isinstance(value, ast.Dict):
                        for key, item in zip(value.keys, value.values):
                            if (isinstance(key, ast.Constant) and key.value == "name"
                                    and isinstance(item, ast.Constant)
                                    and isinstance(item.value, str)):
                                names.add(item.value)
        return names

    def test_every_active_tool_has_explicit_risk_policy(self):
        self.assertEqual(len(self.inline), 8)
        self.assertEqual(len(self.actions), 20)
        self.assertEqual(len(self.plugins), 27)
        self.assertEqual(len(self.active), 55)
        self.assertEqual(unreviewed_tool_names(self.active), [])
        self.assertEqual(set(TOOL_SECURITY), self.active)

    def test_categories_are_from_the_supported_taxonomy(self):
        categories = {row[0] for row in TOOL_SECURITY.values()}
        self.assertLessEqual(categories, {
            "read_only", "reversible_local_action", "external_communication",
            "destructive_action", "financial_or_consequential_action",
            "security_sensitive_action",
        })

    def test_unknown_tool_has_no_policy(self):
        self.assertIsNone(tool_policy("new_unreviewed_plugin"))

    def test_unknown_nested_tool_is_denied_before_dispatch(self):
        rem = main.RemLive.__new__(main.RemLive)
        rem._dispatch_context_local = contextvars.ContextVar(
            f"security-test-{id(rem)}", default=None)
        result = rem._run_tool_for_plugin("unreviewed_tool", {})
        self.assertIn("unreviewed_tool", result)
        self.assertIn("no action was taken", result)

    def test_unknown_live_tool_is_denied_before_registry_dispatch(self):
        rem = main.RemLive.__new__(main.RemLive)
        call = SimpleNamespace(name="unreviewed_tool", args={}, id="security-test")
        coroutine = rem._execute_tool(call, None)
        with self.assertRaises(StopIteration) as completed:
            coroutine.send(None)
        response = completed.exception.value
        self.assertIn("no action was taken", str(response.response))


if __name__ == "__main__":
    unittest.main()
