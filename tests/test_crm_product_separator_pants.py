import ast
import inspect
import json
import shutil
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for path in (ROOT, ROOT / "workers"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import crm_product_separator as separator


CASES = [
    ("G184 Gildan Heavy Blend Sweatpants", "pants"),
    ("Open Bottom Sweat Pant", "pants"),
    ("Sweat-pants", "pants"),
    ("Drawstring Fleece Pants", "pants"),
    ("Youth Joggers", "pants"),
    ("Toddler Sweatpants", "pants"),
    ("Women's Trousers", "pants"),
    ("G500 Gildan Heavy Cotton T-Shirt", "adult_general"),
    ("G185 Gildan Heavy Blend Hoodie", "adult_general"),
    ("Drawstring Bag", "bag"),
    ("Youth Tee", "youth"),
]


class ProductSeparatorPantsTests(unittest.TestCase):
    def test_pants_classification(self):
        for name, expected in CASES:
            with self.subTest(name=name):
                self.assertEqual(separator._classify_product({"product_name": name})["group"], expected)

    def test_mixed_tab_keeps_tops_and_clones_pants(self):
        products = [separator._classify_product({"product_name": name}) for name in (
            "G500 Gildan Heavy Cotton T-Shirt",
            "G184 Gildan Heavy Blend Sweatpants",
            "G185 Gildan Heavy Blend Hoodie",
        )]
        plan = separator._build_separator_plan({"tabs": [{
            "tab_number": 2, "tab_name": "H-Test636", "needs_split": True,
            "products": products,
        }]})
        assignments = plan["split_tabs"][0]["assignments"]
        self.assertEqual([a["keep_group"] for a in assignments], ["adult_general", "pants"])
        self.assertEqual(assignments[1]["source"], "clone")
        self.assertEqual(assignments[1]["keep_product_names"], ["G184 Gildan Heavy Blend Sweatpants"])
        self.assertEqual(len(assignments[0]["keep_product_names"]), 2)

    @unittest.skipUnless(shutil.which("node"), "Node is required to execute browser classifiers")
    def test_live_browser_classifiers_agree_with_scan(self):
        for function, start, end, expression in (
            (separator._zero_non_keep_group_quantity_inputs, "function classifyText(text)",
             "function nearestProductBlock", "classifyText(name)"),
            (separator._keep_only_group_on_design, "function classify(item)",
             "function itemName", "classify({ourLabel: name})"),
        ):
            tree = ast.parse(inspect.getsource(function))
            script = next(n.value for n in ast.walk(tree)
                          if isinstance(n, ast.Constant) and isinstance(n.value, str) and start in n.value)
            classifier = script[script.index(start):script.index(end)]
            program = (
                "function clean(v) { return String(v || '').replace(/\\s+/g, ' ').trim(); }\n"
                + classifier + "\nconst cases = " + json.dumps(CASES) + ";\n"
                + "console.log(JSON.stringify(cases.map(([name]) => " + expression + ")));"
            )
            with self.subTest(function=function.__name__):
                result = subprocess.run(["node", "-e", program], check=True, capture_output=True, text=True)
                self.assertEqual(json.loads(result.stdout), [expected for _, expected in CASES])


if __name__ == "__main__":
    unittest.main()
