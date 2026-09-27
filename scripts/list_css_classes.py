"""List every CSS class referenced by the templates (style/script stripped).

Used to guarantee the reskin's stylesheet covers 100% of the classes the
markup actually uses, so nothing silently falls back to browser defaults.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
src = (ROOT / "llm_evl" / "web" / "index.html").read_text(encoding="utf-8")

body = re.sub(r"<style>.*?</style>", "", src, flags=re.S)
body = re.sub(r"<script.*?</script>", "", body, flags=re.S)

classes: set[str] = set()

# static class="a b c"
for m in re.finditer(r'class="([^"]+)"', body):
    for c in m.group(1).split():
        if c and not c.startswith(("{", "[")):
            classes.add(c)

# dynamic :class="{ cond ? 'a' : 'b' }"  /  :class="['a','b']"
for m in re.finditer(r":class=\"([^\"]+)\"", body):
    for c in re.findall(r"['\"]([a-z][\w-]*)['\"]", m.group(1)):
        classes.add(c)
    # bare identifiers in the expression, e.g. :class="{ foo_cls }"
    for c in re.findall(r"\b([a-z][\w-]*)_cls\b", m.group(1)):
        classes.add(c + "_cls")
    for c in re.findall(r"\b(cell-best|cell-worst|is-active|on)\b", m.group(1)):
        classes.add(c)

# classes produced in JS and bound dynamically
for m in re.finditer(r"['\"]([a-z][\w-]*(?:-[a-z][\w-]*)+)['\"]", body):
    classes.add(m.group(1))

skip = {
    "el-button", "el-table", "el-tag", "el-select", "el-input", "el-dialog",
    "el-form", "el-form-item", "el-table-column", "el-progress", "el-tooltip",
    "el-switch", "el-radio-group", "el-radio-button", "el-input-number",
    "el-option", "el-tab-pane", "el-tabs", "el-checkbox", "el-descriptions",
    "el-empty", "el-scrollbar", "el-alert", "el-icon",
}

named = sorted(c for c in classes if c not in skip and not c.endswith("_cls"))
print(" ".join(named))
print()
print("total", len(named))
if len(sys.argv) > 1:
    Path(sys.argv[1]).write_text("\n".join(named), encoding="utf-8")
