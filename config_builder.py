#!/usr/bin/env python3
"""
Config Builder GUI — Local monitoring rule manager.
Zero pip dependencies: uses only Python standard library.
Generates config.yaml for manual SFTP transfer to VPS.
"""
import tkinter as tk
from tkinter import ttk, messagebox, filedialog, scrolledtext
import json
import os
import re
from datetime import datetime

# ─── YAML Writer (stdlib only, no PyYAML needed) ──────────────────────────────

def _yaml_str(s: str) -> str:
    """Safely quote a YAML string value."""
    if not s:
        return '""'
    # Must quote if contains special YAML chars
    must_quote = any(c in s for c in ':#{}&*!,[]|>\'"@`%')
    must_quote = must_quote or s[0] in '-?' or s.strip() != s
    if must_quote:
        escaped = s.replace('\\', '\\\\').replace('"', '\\"')
        return f'"{escaped}"'
    return s

def _yaml_list_of_str(items: list, indent: int = 4) -> str:
    pad = ' ' * indent
    return '\n'.join(f'{pad}- {_yaml_str(i)}' for i in items) if items else '  []'

def build_yaml(data: dict) -> str:
    """Manually serialize our specific config structure to YAML."""
    lines = ["# Monitoring System Config"]
    lines.append(f"# Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append("")

    # ── global ──
    g = data.get("global", {})
    lines.append("global:")
    lines.append(f"  check_interval_minutes: {int(g.get('check_interval_minutes', 30))}")
    lines.append(f"  heartbeat_interval_hours: {int(g.get('heartbeat_interval_hours', 24))}")
    lines.append(f"  request_timeout: {int(g.get('request_timeout', 30))}")
    lines.append(f"  randomize_delay_min: {float(g.get('randomize_delay_min', 1.0))}")
    lines.append(f"  randomize_delay_max: {float(g.get('randomize_delay_max', 3.0))}")

    # apprise_urls list
    urls = g.get("apprise_urls", [])
    if urls:
        lines.append("  apprise_urls:")
        for u in urls:
            lines.append(f"    - {_yaml_str(u)}")
    else:
        lines.append("  apprise_urls: []")

    lines.append("")

    # ── targets ──
    targets = data.get("targets", [])
    lines.append("targets:")
    if not targets:
        lines.append("  []")
    for t in targets:
        lines.append(f"  - name: {_yaml_str(t.get('name', ''))}")
        lines.append(f"    url: {_yaml_str(t.get('url', ''))}")
        mode = t.get('detection_mode', 'selector_disappears')
        lines.append(f"    detection_mode: {_yaml_str(mode)}")
        selector = t.get('css_selector', '')
        if selector:
            lines.append(f"    css_selector: {_yaml_str(selector)}")
        proxy = t.get('proxy', '')
        if proxy:
            lines.append(f"    proxy: {_yaml_str(proxy)}")
        interval = t.get('check_interval_minutes', '')
        if interval:
            lines.append(f"    check_interval_minutes: {int(interval)}")
        enabled = t.get('enabled', True)
        lines.append(f"    enabled: {'true' if enabled else 'false'}")
    lines.append("")

    # ── rss_feeds ──
    feeds = data.get("rss_feeds", [])
    lines.append("rss_feeds:")
    if not feeds:
        lines.append("  []")
    for f in feeds:
        lines.append(f"  - name: {_yaml_str(f.get('name', ''))}")
        lines.append(f"    url: {_yaml_str(f.get('url', ''))}")
        kws = f.get('keywords', [])
        if kws:
            lines.append("    keywords:")
            for kw in kws:
                lines.append(f"      - {_yaml_str(kw)}")
        else:
            lines.append("    keywords: []")
        interval = f.get('check_interval_minutes', '')
        if interval:
            lines.append(f"    check_interval_minutes: {int(interval)}")
        enabled = f.get('enabled', True)
        lines.append(f"    enabled: {'true' if enabled else 'false'}")

    return '\n'.join(lines) + '\n'

# ─── Color Theme ──────────────────────────────────────────────────────────────

DARK  = "#0d1117"
DARK2 = "#161b22"
DARK3 = "#21262d"
BORDER= "#30363d"
TEXT  = "#e6edf3"
TEXT2 = "#8b949e"
ACCENT= "#58a6ff"
GREEN = "#3fb950"
RED   = "#f85149"
ORANGE= "#d29922"

# ─── Reusable Widgets ─────────────────────────────────────────────────────────

class StyledButton(tk.Button):
    def __init__(self, parent, text, command=None, color=ACCENT, **kw):
        super().__init__(
            parent, text=text, command=command,
            bg=color, fg=DARK, relief="flat", bd=0,
            padx=12, pady=6,
            font=("Helvetica", 10, "bold"),
            cursor="hand2", **kw
        )
        self._color = color
        self.bind("<Enter>", lambda e: self.config(bg=self._lighten(color)))
        self.bind("<Leave>", lambda e: self.config(bg=color))

    @staticmethod
    def _lighten(hex_color):
        h = hex_color.lstrip('#')
        r, g, b = (int(h[i:i+2], 16) for i in (0, 2, 4))
        r = min(255, r + 30); g = min(255, g + 30); b = min(255, b + 30)
        return f'#{r:02x}{g:02x}{b:02x}'

class LabeledEntry(tk.Frame):
    def __init__(self, parent, label, placeholder="", width=40, **kw):
        super().__init__(parent, bg=DARK2, **kw)
        tk.Label(self, text=label, bg=DARK2, fg=TEXT2,
                 font=("Helvetica", 9)).pack(anchor="w")
        self.var = tk.StringVar()
        self.entry = tk.Entry(
            self, textvariable=self.var, width=width,
            bg=DARK3, fg=TEXT, insertbackground=TEXT,
            relief="flat", bd=0, highlightthickness=1,
            highlightbackground=BORDER, highlightcolor=ACCENT,
            font=("Courier", 10)
        )
        self.entry.pack(fill="x", ipady=5, padx=1)
        if placeholder:
            self._placeholder = placeholder
            self.entry.insert(0, placeholder)
            self.entry.config(fg=TEXT2)
            self.entry.bind("<FocusIn>", self._on_focus_in)
            self.entry.bind("<FocusOut>", self._on_focus_out)
        else:
            self._placeholder = None

    def _on_focus_in(self, _):
        if self.var.get() == self._placeholder:
            self.entry.delete(0, "end")
            self.entry.config(fg=TEXT)

    def _on_focus_out(self, _):
        if not self.var.get():
            self.entry.insert(0, self._placeholder)
            self.entry.config(fg=TEXT2)

    def get(self):
        v = self.var.get()
        return "" if v == self._placeholder else v

    def set(self, v):
        self.var.set(v)
        if self._placeholder:
            self.entry.config(fg=TEXT if v else TEXT2)

# ─── Dialog Windows ───────────────────────────────────────────────────────────

class TargetDialog(tk.Toplevel):
    """Add/Edit a VPS stock-check target."""

    MODES = {
        "selector_disappears": "Selector DISAPPEARS → In Stock  (e.g. 'Out of Stock' badge gone)",
        "selector_appears":    "Selector APPEARS → In Stock  (e.g. 'Buy Now' button shows up)",
        "content_hash":        "Any Page Change → Alert  (no selector needed)",
    }

    def __init__(self, parent, existing=None):
        super().__init__(parent)
        self.title("VPS Target" if not existing else "Edit Target")
        self.configure(bg=DARK2)
        self.resizable(False, False)
        self.result = None

        pad = dict(padx=16, pady=6)

        tk.Label(self, text="Target Name", bg=DARK2, fg=TEXT2,
                 font=("Helvetica", 9)).pack(anchor="w", **pad)
        self.name = tk.Entry(self, bg=DARK3, fg=TEXT, insertbackground=TEXT,
                             relief="flat", bd=0, highlightthickness=1,
                             highlightbackground=BORDER, highlightcolor=ACCENT,
                             font=("Courier", 10), width=50)
        self.name.pack(fill="x", padx=16, ipady=5)

        tk.Label(self, text="URL", bg=DARK2, fg=TEXT2,
                 font=("Helvetica", 9)).pack(anchor="w", **pad)
        self.url = tk.Entry(self, bg=DARK3, fg=TEXT, insertbackground=TEXT,
                            relief="flat", bd=0, highlightthickness=1,
                            highlightbackground=BORDER, highlightcolor=ACCENT,
                            font=("Courier", 10), width=50)
        self.url.pack(fill="x", padx=16, ipady=5)

        tk.Label(self, text="Detection Mode", bg=DARK2, fg=TEXT2,
                 font=("Helvetica", 9)).pack(anchor="w", **pad)
        self.mode_var = tk.StringVar(value="selector_disappears")
        for key, label in self.MODES.items():
            tk.Radiobutton(
                self, text=label, variable=self.mode_var, value=key,
                bg=DARK2, fg=TEXT, selectcolor=DARK3,
                activebackground=DARK2, activeforeground=ACCENT,
                font=("Helvetica", 9)
            ).pack(anchor="w", padx=16)

        tk.Label(self, text="CSS Selector  (leave blank for content_hash mode)",
                 bg=DARK2, fg=TEXT2, font=("Helvetica", 9)).pack(anchor="w", **pad)
        self.selector = tk.Entry(self, bg=DARK3, fg=TEXT, insertbackground=TEXT,
                                 relief="flat", bd=0, highlightthickness=1,
                                 highlightbackground=BORDER, highlightcolor=ACCENT,
                                 font=("Courier", 10), width=50)
        self.selector.pack(fill="x", padx=16, ipady=5)
        tk.Label(self, text='  e.g.  .out-of-stock   or   button[data-action="add-to-cart"]',
                 bg=DARK2, fg=TEXT2, font=("Helvetica", 8)).pack(anchor="w", padx=16)

        tk.Label(self, text="Proxy  (optional: socks5://user:pass@host:port)",
                 bg=DARK2, fg=TEXT2, font=("Helvetica", 9)).pack(anchor="w", **pad)
        self.proxy = tk.Entry(self, bg=DARK3, fg=TEXT, insertbackground=TEXT,
                              relief="flat", bd=0, highlightthickness=1,
                              highlightbackground=BORDER, highlightcolor=ACCENT,
                              font=("Courier", 10), width=50)
        self.proxy.pack(fill="x", padx=16, ipady=5)

        tk.Label(self, text="Check Interval Override (minutes, leave blank = global)",
                 bg=DARK2, fg=TEXT2, font=("Helvetica", 9)).pack(anchor="w", **pad)
        self.interval = tk.Entry(self, bg=DARK3, fg=TEXT, insertbackground=TEXT,
                                 relief="flat", bd=0, highlightthickness=1,
                                 highlightbackground=BORDER, highlightcolor=ACCENT,
                                 font=("Courier", 10), width=12)
        self.interval.pack(anchor="w", padx=16, ipady=5)

        self.enabled_var = tk.BooleanVar(value=True)
        tk.Checkbutton(self, text="Enabled", variable=self.enabled_var,
                       bg=DARK2, fg=TEXT, selectcolor=DARK3,
                       activebackground=DARK2, activeforeground=ACCENT,
                       font=("Helvetica", 10)).pack(anchor="w", padx=16, pady=4)

        btn_frame = tk.Frame(self, bg=DARK2)
        btn_frame.pack(fill="x", padx=16, pady=12)
        StyledButton(btn_frame, "Save", self._save, color=GREEN).pack(side="left")
        StyledButton(btn_frame, "Cancel", self.destroy, color=BORDER).pack(side="left", padx=8)

        if existing:
            self.name.insert(0, existing.get("name", ""))
            self.url.insert(0, existing.get("url", ""))
            self.mode_var.set(existing.get("detection_mode", "selector_disappears"))
            self.selector.insert(0, existing.get("css_selector", ""))
            self.proxy.insert(0, existing.get("proxy", ""))
            if existing.get("check_interval_minutes"):
                self.interval.insert(0, str(existing["check_interval_minutes"]))
            self.enabled_var.set(existing.get("enabled", True))

        self.grab_set()
        self.wait_window()

    def _save(self):
        name = self.name.get().strip()
        url  = self.url.get().strip()
        if not name or not url:
            messagebox.showerror("Validation", "Name and URL are required.", parent=self)
            return
        if not url.startswith("http"):
            messagebox.showerror("Validation", "URL must start with http:// or https://", parent=self)
            return
        interval = self.interval.get().strip()
        self.result = {
            "name": name,
            "url": url,
            "detection_mode": self.mode_var.get(),
            "css_selector": self.selector.get().strip(),
            "proxy": self.proxy.get().strip(),
            "check_interval_minutes": int(interval) if interval.isdigit() else None,
            "enabled": self.enabled_var.get(),
        }
        # Clean None values
        self.result = {k: v for k, v in self.result.items() if v not in (None, "")}
        self.result["enabled"] = self.enabled_var.get()
        self.destroy()


class RSSDialog(tk.Toplevel):
    """Add/Edit an RSS feed subscription."""

    def __init__(self, parent, existing=None):
        super().__init__(parent)
        self.title("RSS Feed" if not existing else "Edit RSS Feed")
        self.configure(bg=DARK2)
        self.resizable(False, False)
        self.result = None

        pad = dict(padx=16, pady=6)

        tk.Label(self, text="Feed Name", bg=DARK2, fg=TEXT2,
                 font=("Helvetica", 9)).pack(anchor="w", **pad)
        self.name = tk.Entry(self, bg=DARK3, fg=TEXT, insertbackground=TEXT,
                             relief="flat", bd=0, highlightthickness=1,
                             highlightbackground=BORDER, highlightcolor=ACCENT,
                             font=("Courier", 10), width=50)
        self.name.pack(fill="x", padx=16, ipady=5)

        tk.Label(self, text="Feed URL", bg=DARK2, fg=TEXT2,
                 font=("Helvetica", 9)).pack(anchor="w", **pad)
        self.url = tk.Entry(self, bg=DARK3, fg=TEXT, insertbackground=TEXT,
                            relief="flat", bd=0, highlightthickness=1,
                            highlightbackground=BORDER, highlightcolor=ACCENT,
                            font=("Courier", 10), width=50)
        self.url.pack(fill="x", padx=16, ipady=5)

        tk.Label(self, text="Keywords Filter  (comma-separated, blank = all items)",
                 bg=DARK2, fg=TEXT2, font=("Helvetica", 9)).pack(anchor="w", **pad)
        self.keywords = tk.Entry(self, bg=DARK3, fg=TEXT, insertbackground=TEXT,
                                 relief="flat", bd=0, highlightthickness=1,
                                 highlightbackground=BORDER, highlightcolor=ACCENT,
                                 font=("Courier", 10), width=50)
        self.keywords.pack(fill="x", padx=16, ipady=5)
        tk.Label(self, text='  e.g.  annual, KVM, 512MB, special offer',
                 bg=DARK2, fg=TEXT2, font=("Helvetica", 8)).pack(anchor="w", padx=16)

        tk.Label(self, text="Check Interval Override (minutes, leave blank = global)",
                 bg=DARK2, fg=TEXT2, font=("Helvetica", 9)).pack(anchor="w", **pad)
        self.interval = tk.Entry(self, bg=DARK3, fg=TEXT, insertbackground=TEXT,
                                 relief="flat", bd=0, highlightthickness=1,
                                 highlightbackground=BORDER, highlightcolor=ACCENT,
                                 font=("Courier", 10), width=12)
        self.interval.pack(anchor="w", padx=16, ipady=5)

        self.enabled_var = tk.BooleanVar(value=True)
        tk.Checkbutton(self, text="Enabled", variable=self.enabled_var,
                       bg=DARK2, fg=TEXT, selectcolor=DARK3,
                       activebackground=DARK2, activeforeground=ACCENT,
                       font=("Helvetica", 10)).pack(anchor="w", padx=16, pady=4)

        btn_frame = tk.Frame(self, bg=DARK2)
        btn_frame.pack(fill="x", padx=16, pady=12)
        StyledButton(btn_frame, "Save", self._save, color=GREEN).pack(side="left")
        StyledButton(btn_frame, "Cancel", self.destroy, color=BORDER).pack(side="left", padx=8)

        if existing:
            self.name.insert(0, existing.get("name", ""))
            self.url.insert(0, existing.get("url", ""))
            kws = existing.get("keywords", [])
            if kws:
                self.keywords.insert(0, ", ".join(kws))
            if existing.get("check_interval_minutes"):
                self.interval.insert(0, str(existing["check_interval_minutes"]))
            self.enabled_var.set(existing.get("enabled", True))

        self.grab_set()
        self.wait_window()

    def _save(self):
        name = self.name.get().strip()
        url  = self.url.get().strip()
        if not name or not url:
            messagebox.showerror("Validation", "Name and URL are required.", parent=self)
            return
        kw_raw = self.keywords.get().strip()
        keywords = [k.strip() for k in kw_raw.split(",") if k.strip()] if kw_raw else []
        interval = self.interval.get().strip()
        self.result = {
            "name": name,
            "url": url,
            "keywords": keywords,
            "enabled": self.enabled_var.get(),
        }
        if interval.isdigit():
            self.result["check_interval_minutes"] = int(interval)
        self.destroy()


# ─── Main Application ─────────────────────────────────────────────────────────

class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Monitoring Config Builder")
        self.configure(bg=DARK)
        self.minsize(820, 640)
        self._targets: list[dict] = []
        self._feeds:   list[dict] = []
        self._build_ui()
        self._update_lists()

    # ── Layout ──────────────────────────────────────────────────────────────

    def _build_ui(self):
        # ── Header ──
        header = tk.Frame(self, bg=DARK2, pady=0)
        header.pack(fill="x")
        tk.Label(header, text="⬡  Monitoring Config Builder",
                 bg=DARK2, fg=ACCENT, font=("Helvetica", 15, "bold"),
                 padx=20, pady=14).pack(side="left")
        tk.Label(header, text="generates config.yaml → SFTP to VPS",
                 bg=DARK2, fg=TEXT2, font=("Helvetica", 10)).pack(side="left")

        # ── Body ──
        body = tk.Frame(self, bg=DARK)
        body.pack(fill="both", expand=True, padx=20, pady=16)

        # Left column
        left = tk.Frame(body, bg=DARK)
        left.pack(side="left", fill="both", expand=True, padx=(0, 10))

        self._build_global(left)
        self._build_targets(left)

        # Right column
        right = tk.Frame(body, bg=DARK)
        right.pack(side="left", fill="both", expand=True)

        self._build_rss(right)
        self._build_preview(right)

        # ── Footer buttons ──
        footer = tk.Frame(self, bg=DARK2)
        footer.pack(fill="x", side="bottom")
        StyledButton(footer, "📥  Export config.yaml", self._export, color=GREEN).pack(
            side="right", padx=16, pady=10)
        StyledButton(footer, "📂  Load config.yaml", self._load, color=ORANGE).pack(
            side="right", pady=10)
        StyledButton(footer, "👁  Preview YAML", self._show_preview, color=ACCENT).pack(
            side="right", padx=8, pady=10)

    def _section(self, parent, title):
        f = tk.Frame(parent, bg=DARK2, relief="flat",
                     highlightthickness=1, highlightbackground=BORDER)
        f.pack(fill="both", expand=False, pady=(0, 12))
        tk.Label(f, text=title, bg=DARK2, fg=ACCENT,
                 font=("Helvetica", 11, "bold"), padx=12, pady=8).pack(anchor="w")
        tk.Frame(f, bg=BORDER, height=1).pack(fill="x")
        inner = tk.Frame(f, bg=DARK2, padx=12, pady=10)
        inner.pack(fill="both", expand=True)
        return inner

    # ── Global Settings ──────────────────────────────────────────────────────

    def _build_global(self, parent):
        inner = self._section(parent, "⚙  Global Settings")

        # Apprise URLs
        tk.Label(inner, text="Notification URLs  (one per line)  — supports tgram://, mailto://, slack:// etc.",
                 bg=DARK2, fg=TEXT2, font=("Helvetica", 9)).pack(anchor="w")
        self.apprise_text = tk.Text(inner, height=4, bg=DARK3, fg=TEXT,
                                    insertbackground=TEXT, relief="flat", bd=0,
                                    highlightthickness=1, highlightbackground=BORDER,
                                    highlightcolor=ACCENT, font=("Courier", 10))
        self.apprise_text.pack(fill="x", ipady=4, pady=(0, 8))
        tk.Label(inner, text="  tgram://BotToken/ChatID   |   mailto://user:pass@gmail.com",
                 bg=DARK2, fg=TEXT2, font=("Helvetica", 8)).pack(anchor="w", pady=(0, 8))

        row = tk.Frame(inner, bg=DARK2)
        row.pack(fill="x", pady=4)

        def num_entry(label, default, width=8):
            f = tk.Frame(row, bg=DARK2)
            f.pack(side="left", padx=(0, 16))
            tk.Label(f, text=label, bg=DARK2, fg=TEXT2,
                     font=("Helvetica", 9)).pack(anchor="w")
            e = tk.Entry(f, width=width, bg=DARK3, fg=TEXT,
                         insertbackground=TEXT, relief="flat", bd=0,
                         highlightthickness=1, highlightbackground=BORDER,
                         highlightcolor=ACCENT, font=("Courier", 10))
            e.insert(0, str(default))
            e.pack(ipady=5)
            return e

        self.interval_entry   = num_entry("Check Interval (min)", 30)
        self.heartbeat_entry  = num_entry("Heartbeat (hours)", 24)
        self.timeout_entry    = num_entry("Request Timeout (s)", 30)
        self.delay_min_entry  = num_entry("Delay Min (s)", 1.0, 6)
        self.delay_max_entry  = num_entry("Delay Max (s)", 3.0, 6)

    # ── VPS Targets ──────────────────────────────────────────────────────────

    def _build_targets(self, parent):
        sec = self._section(parent, "🖥  VPS Stock Check Targets")

        # Toolbar
        tb = tk.Frame(sec, bg=DARK2)
        tb.pack(fill="x", pady=(0, 8))
        StyledButton(tb, "+ Add Target", self._add_target, color=ACCENT).pack(side="left")
        StyledButton(tb, "✎ Edit", self._edit_target, color=DARK3).pack(side="left", padx=6)
        StyledButton(tb, "✕ Remove", self._del_target, color=RED).pack(side="left")

        # List
        cols = ("name", "mode", "interval", "enabled")
        self.target_tree = ttk.Treeview(sec, columns=cols, show="headings",
                                        height=6, style="Dark.Treeview")
        self.target_tree.heading("name",     text="Name")
        self.target_tree.heading("mode",     text="Mode")
        self.target_tree.heading("interval", text="Interval")
        self.target_tree.heading("enabled",  text="On")
        self.target_tree.column("name",     width=180)
        self.target_tree.column("mode",     width=160)
        self.target_tree.column("interval", width=70)
        self.target_tree.column("enabled",  width=40)
        self.target_tree.pack(fill="both", expand=True)
        self.target_tree.bind("<Double-1>", lambda _: self._edit_target())
        self._style_tree()

    # ── RSS Feeds ────────────────────────────────────────────────────────────

    def _build_rss(self, parent):
        sec = self._section(parent, "📡  RSS Feed Subscriptions")

        tb = tk.Frame(sec, bg=DARK2)
        tb.pack(fill="x", pady=(0, 8))
        StyledButton(tb, "+ Add Feed", self._add_feed, color=ACCENT).pack(side="left")
        StyledButton(tb, "✎ Edit",    self._edit_feed, color=DARK3).pack(side="left", padx=6)
        StyledButton(tb, "✕ Remove",  self._del_feed,  color=RED).pack(side="left")

        cols = ("name", "keywords", "interval", "enabled")
        self.feed_tree = ttk.Treeview(sec, columns=cols, show="headings",
                                      height=6, style="Dark.Treeview")
        self.feed_tree.heading("name",     text="Name")
        self.feed_tree.heading("keywords", text="Keywords")
        self.feed_tree.heading("interval", text="Interval")
        self.feed_tree.heading("enabled",  text="On")
        self.feed_tree.column("name",     width=150)
        self.feed_tree.column("keywords", width=200)
        self.feed_tree.column("interval", width=70)
        self.feed_tree.column("enabled",  width=40)
        self.feed_tree.pack(fill="both", expand=True)
        self.feed_tree.bind("<Double-1>", lambda _: self._edit_feed())

    # ── YAML Preview ─────────────────────────────────────────────────────────

    def _build_preview(self, parent):
        sec = self._section(parent, "📄  YAML Preview")
        self.preview = scrolledtext.ScrolledText(
            sec, height=10, bg=DARK3, fg=GREEN,
            insertbackground=TEXT, relief="flat", bd=0,
            font=("Courier", 9), state="disabled"
        )
        self.preview.pack(fill="both", expand=True)

    # ── Tree Style ───────────────────────────────────────────────────────────

    def _style_tree(self):
        style = ttk.Style(self)
        style.theme_use("default")
        style.configure("Dark.Treeview",
                         background=DARK3, foreground=TEXT,
                         fieldbackground=DARK3, rowheight=26,
                         borderwidth=0, relief="flat")
        style.configure("Dark.Treeview.Heading",
                         background=DARK2, foreground=TEXT2,
                         borderwidth=0, relief="flat",
                         font=("Helvetica", 9, "bold"))
        style.map("Dark.Treeview",
                  background=[("selected", DARK)],
                  foreground=[("selected", ACCENT)])
        # Apply to feed tree when it's created
        if hasattr(self, "feed_tree"):
            self.feed_tree.configure(style="Dark.Treeview")

    # ── Data Operations ──────────────────────────────────────────────────────

    def _add_target(self):
        d = TargetDialog(self)
        if d.result:
            self._targets.append(d.result)
            self._update_lists()

    def _edit_target(self):
        sel = self.target_tree.selection()
        if not sel:
            return
        idx = self.target_tree.index(sel[0])
        d = TargetDialog(self, existing=self._targets[idx])
        if d.result:
            self._targets[idx] = d.result
            self._update_lists()

    def _del_target(self):
        sel = self.target_tree.selection()
        if not sel:
            return
        idx = self.target_tree.index(sel[0])
        name = self._targets[idx]["name"]
        if messagebox.askyesno("Confirm", f"Remove target '{name}'?", parent=self):
            self._targets.pop(idx)
            self._update_lists()

    def _add_feed(self):
        d = RSSDialog(self)
        if d.result:
            self._feeds.append(d.result)
            self._update_lists()

    def _edit_feed(self):
        sel = self.feed_tree.selection()
        if not sel:
            return
        idx = self.feed_tree.index(sel[0])
        d = RSSDialog(self, existing=self._feeds[idx])
        if d.result:
            self._feeds[idx] = d.result
            self._update_lists()

    def _del_feed(self):
        sel = self.feed_tree.selection()
        if not sel:
            return
        idx = self.feed_tree.index(sel[0])
        name = self._feeds[idx]["name"]
        if messagebox.askyesno("Confirm", f"Remove feed '{name}'?", parent=self):
            self._feeds.pop(idx)
            self._update_lists()

    def _update_lists(self):
        # Targets
        for item in self.target_tree.get_children():
            self.target_tree.delete(item)
        for t in self._targets:
            mode = t.get("detection_mode", "selector_disappears")
            short_mode = {"selector_disappears": "disappears", "selector_appears": "appears",
                          "content_hash": "hash-diff"}.get(mode, mode)
            interval = str(t.get("check_interval_minutes", "global"))
            enabled = "✓" if t.get("enabled", True) else "✗"
            self.target_tree.insert("", "end", values=(
                t.get("name", ""), short_mode, interval, enabled
            ))

        # Feeds
        for item in self.feed_tree.get_children():
            self.feed_tree.delete(item)
        for f in self._feeds:
            kws = ", ".join(f.get("keywords", [])) or "(all)"
            interval = str(f.get("check_interval_minutes", "global"))
            enabled = "✓" if f.get("enabled", True) else "✗"
            self.feed_tree.insert("", "end", values=(
                f.get("name", ""), kws, interval, enabled
            ))

    def _build_config(self) -> dict:
        urls_raw = self.apprise_text.get("1.0", "end").strip()
        apprise_urls = [u.strip() for u in urls_raw.splitlines() if u.strip()]
        return {
            "global": {
                "apprise_urls": apprise_urls,
                "check_interval_minutes": self.interval_entry.get() or "30",
                "heartbeat_interval_hours": self.heartbeat_entry.get() or "24",
                "request_timeout": self.timeout_entry.get() or "30",
                "randomize_delay_min": self.delay_min_entry.get() or "1.0",
                "randomize_delay_max": self.delay_max_entry.get() or "3.0",
            },
            "targets": self._targets,
            "rss_feeds": self._feeds,
        }

    def _show_preview(self):
        yaml_str = build_yaml(self._build_config())
        self.preview.config(state="normal")
        self.preview.delete("1.0", "end")
        self.preview.insert("1.0", yaml_str)
        self.preview.config(state="disabled")

    def _export(self):
        cfg = self._build_config()
        if not cfg["global"]["apprise_urls"]:
            if not messagebox.askyesno(
                "Warning",
                "No notification URLs set. Workers will scrape but never alert.\nExport anyway?",
                parent=self
            ):
                return
        path = filedialog.asksaveasfilename(
            defaultextension=".yaml",
            filetypes=[("YAML files", "*.yaml *.yml"), ("All files", "*.*")],
            initialfile="config.yaml",
            title="Export config.yaml"
        )
        if not path:
            return
        try:
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(build_yaml(cfg))
            messagebox.showinfo(
                "Exported",
                f"Saved to:\n{path}\n\nSFTP this file to your VPS at the same path as worker.py",
                parent=self
            )
        except Exception as exc:
            messagebox.showerror("Error", str(exc), parent=self)

    def _load(self):
        """Load an existing config.yaml back into the GUI."""
        path = filedialog.askopenfilename(
            filetypes=[("YAML files", "*.yaml *.yml"), ("All files", "*.*")],
            title="Load config.yaml"
        )
        if not path:
            return
        try:
            # Minimal YAML parser for our known structure
            self._load_yaml_file(path)
        except Exception as exc:
            messagebox.showerror("Load Error", str(exc), parent=self)

    def _load_yaml_file(self, path: str):
        """Very basic YAML loader for our config format (no PyYAML needed)."""
        import re
        with open(path, encoding="utf-8") as fh:
            content = fh.read()

        # We'll use a simple line-by-line parser for our known structure
        # Fall back gracefully - just try to use eval-safe parsing
        try:
            import importlib
            yaml_mod = importlib.import_module("yaml")
            data = yaml_mod.safe_load(content)
        except ImportError:
            # Minimal manual parse — enough for round-trip of our own output
            data = self._simple_yaml_parse(content)

        g = data.get("global", {})
        urls = g.get("apprise_urls", [])
        self.apprise_text.delete("1.0", "end")
        self.apprise_text.insert("1.0", "\n".join(urls))

        def _set(entry, val):
            entry.delete(0, "end")
            entry.insert(0, str(val))

        _set(self.interval_entry,  g.get("check_interval_minutes", 30))
        _set(self.heartbeat_entry, g.get("heartbeat_interval_hours", 24))
        _set(self.timeout_entry,   g.get("request_timeout", 30))
        _set(self.delay_min_entry, g.get("randomize_delay_min", 1.0))
        _set(self.delay_max_entry, g.get("randomize_delay_max", 3.0))

        self._targets = data.get("targets", []) or []
        self._feeds   = data.get("rss_feeds", []) or []
        self._update_lists()
        messagebox.showinfo("Loaded", f"Loaded {len(self._targets)} targets and "
                            f"{len(self._feeds)} RSS feeds.", parent=self)

    def _simple_yaml_parse(self, text: str) -> dict:
        """Fallback minimal parser — only needed if PyYAML not installed."""
        raise NotImplementedError(
            "PyYAML not installed. Install it for Load support: pip install pyyaml\n"
            "Export still works without PyYAML."
        )


# ─── Entry Point ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
    app = App()
    app.mainloop()
