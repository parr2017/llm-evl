"""Feature inventory for the web UI.

Parses index.html and extracts everything a user can actually touch: nav
entries, pages, buttons, dialogs, API endpoints, and template bindings.
Run before a reskin and after, then diff the two JSON files to prove the
reskin changed no functionality.

    python scripts/ui_inventory.py before > /tmp/before.json
    python scripts/ui_inventory.py after  > /tmp/after.json
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HTML = ROOT / "llm_evl" / "web" / "index.html"


def main() -> int:
    out_path = sys.argv[1] if len(sys.argv) > 1 else ""
    src = HTML.read_text(encoding="utf-8")

    # --- navigation / pages -------------------------------------------
    # navGroups entries look like {k:'dashboard', i:'◎', l:'概览', ...}
    nav = sorted({
        m.group(1)
        for m in re.finditer(r"\{\s*k:\s*'([a-z0-9\-]+)'\s*,\s*i:", src)
    })
    # pages actually implemented as templates
    pages = sorted({
        m.group(1)
        for m in re.finditer(r"v-if=\"page==='([a-z0-9\-]+)'\"", src)
    })
    # page metadata map
    page_meta = sorted({
        m.group(1)
        for m in re.finditer(r"^\s*'([a-z\-]+)':\s*\{", src, re.M)
    })

    # --- interactive elements ------------------------------------------
    def count_all(pattern: str) -> int:
        return len(re.findall(pattern, src))

    buttons = count_all(r"<el-button\b")
    native_buttons = count_all(r"<button\b")
    selects = count_all(r"<el-select\b")
    inputs = count_all(r"<el-(input|input-number)\b")
    switches = count_all(r"<el-switch\b")
    radios = count_all(r"<el-radio-button\b")
    dialogs = count_all(r"<el-dialog\b")
    tables = count_all(r"<el-table\b")
    columns = count_all(r"<el-table-column\b")
    expand_rows = count_all(r'type="expand"')
    tabs = count_all(r"<el-tabs\b")
    tooltips = count_all(r"<el-tooltip\b")
    progress = count_all(r"<el-progress\b")
    tags = count_all(r"<el-tag\b")
    sliders = count_all(r"<el-slider\b")
    checkboxes = count_all(r"<el-checkbox\b")

    # --- backend surface -----------------------------------------------
    api_paths = sorted({
        m.group(1) + (m.group(2) or "")
        for m in re.finditer(r"['\"`](/api/[a-z0-9\-_/]*)(\$\{|`|\"|')", src)
    })
    event_source = sorted({
        m.group(1)
        for m in re.finditer(r"EventSource\(['\"`]([^'\"`]+)", src)
    })

    # --- template bindings referenced in the template -------------------
    bindings = sorted({
        m.group(1) or m.group(2)
        for m in re.finditer(r"\bv-(?:if|show|for|model|html|bind):[\"']?([a-zA-Z_$][\w$]*)", src)
    })
    mustaches = sorted({
        m.group(1).strip()
        for m in re.finditer(r"\{\{\s*([a-zA-Z_$][\w$.\[\] ]*?)\s*\}\}", src)
    })
    handlers = sorted({
        m.group(1)
        for m in re.finditer(r"@(?:click|change|keydown|submit|close|expand-change)="
                            r"[\"']?([a-zA-Z_$][\w$]*)", src)
    })

    # --- css hygiene ----------------------------------------------------
    style = re.search(r"<style>(.*?)</style>", src, re.S)
    css = style.group(1) if style else ""
    hardcoded_hex = sorted({
        c.lower()
        for c in re.findall(r"#[0-9a-fA-F]{3,8}\b", css)
        if c.lower() not in {
            "#fff", "#ffffff", "#000", "#000000", "#04121f", "#04140d", "#1a0505",
        }
    })
    css_vars = sorted({
        m.group(1) for m in re.finditer(r"(--[\w-]+)\s*:", css)
    })
    # emoji / pictographs that should not appear in a professional UI
    emoji = sorted({
        ch for ch in src
        if ord(ch) >= 0x1F000
        or ch in "\u26a0\u2139\u2714\u2716\u2713\u2728\u2b50"
    })

    out = {
        "nav": nav,
        "pages": pages,
        "controls": {
            "el_button": buttons,
            "native_button": native_buttons,
            "el_select": selects,
            "el_input": inputs,
            "el_switch": switches,
            "el_radio_button": radios,
            "el_dialog": dialogs,
            "el_table": tables,
            "el_table_column": columns,
            "expand_row": expand_rows,
            "el_tabs": tabs,
            "el_tooltip": tooltips,
            "el_progress": progress,
            "el_tag": tags,
            "el_slider": sliders,
            "el_checkbox": checkboxes,
        },
        "api_paths": api_paths,
        "event_sources": event_source,
        "handlers": handlers,
        "n_bindings": len(bindings),
        "n_mustaches": len(mustaches),
        "handlers_count": len(handlers),
        "css": {
            "n_rules": len(re.findall(r"\{", css)),
            "hardcoded_hex_count": len(hardcoded_hex),
            "n_css_vars": len(css_vars),
        },
        "emoji": emoji,
    }
    text = json.dumps(out, ensure_ascii=False, indent=2)
    if out_path:
        Path(out_path).write_text(text, encoding="utf-8")
        print(f"wrote {out_path} ({len(text)} bytes)")
    else:
        print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
