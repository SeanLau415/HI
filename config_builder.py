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
    """Serialize a string as a double-quoted YAML scalar.

    Always quoting avoids YAML 1.1 implicit type coercion such as:
    on/off/yes/no/null/123 -> bool/None/int after safe_load().
    """
    if s is None:
        s = ""
    s = str(s)
    escaped = (
        s.replace('\\', '\\\\')
         .replace('"', '\"')
         .replace('\n', '\\n')
         .replace('\r', '\\r')
         .replace('\t', '\\t')
    )
    return f'"{escaped}"'


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
        open_url = t.get('open_url', '')
        if open_url:
            lines.append(f"    open_url: {_yaml_str(open_url)}")
        mode = t.get('detection_mode', 'selector_disappears')
        lines.append(f"    detection_mode: {_yaml_str(mode)}")
        selector = t.get('css_selector', '')
        if selector:
            lines.append(f"    css_selector: {_yaml_str(selector)}")
        proxy = t.get('proxy', '')
        if proxy:
            lines.append(f"    proxy: {_yaml_str(proxy)}")
        interval = t.get('check_interval_minutes', '')
        if interval is not None and interval != '':
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
        proxy = f.get('proxy', '')
        if proxy:
            lines.append(f"    proxy: {_yaml_str(proxy)}")
        interval = f.get('check_interval_minutes', '')
        if interval is not None and interval != '':
            lines.append(f"    check_interval_minutes: {int(interval)}")
        enabled = f.get('enabled', True)
        lines.append(f"    enabled: {'true' if enabled else 'false'}")

    return '\n'.join(lines) + '\n'


PRESET_PRECISE_TARGETS = [
    {
        "name": "BuyVM LV RYZEN KVM 512MB",
        "url": "https://my.frantech.ca/cart.php?a=add&pid=1439",
        "detection_mode": "content_hash",
        "check_interval_minutes": 10,
        "enabled": True,
    },
    {
        "name": "BuyVM LV RYZEN KVM 1GB",
        "url": "https://my.frantech.ca/cart.php?a=add&pid=1411",
        "detection_mode": "content_hash",
        "check_interval_minutes": 10,
        "enabled": True,
    },
    {
        "name": "Nube FRA1 China-Optimized 1c1g",
        "url": "https://fra-api.nube.sh/order/v1/order/product/info?businessType=VM&productId=&region=FRA",
        "open_url": "https://nube.sh/zh-cn",
        "detection_mode": "content_hash",
        "check_interval_minutes": 10,
        "enabled": True,
    },
    {
        "name": "VMISS US.LA.TRI.Basic",
        "url": "https://app.vmiss.com/store/us-los-angeles-tri/basic",
        "detection_mode": "content_hash",
        "check_interval_minutes": 10,
        "enabled": True,
    },
]


PRESET_UNIVERSITY_FEEDS = [
    {
        "name": "UCLA Extension Program News",
        "url": "https://newsroom.uclaextension.edu/cats/program_news.xml",
        "keywords": ["course", "courses", "class", "certificate", "training", "career technical education", "vetjet", "olli", "paralegal"],
        "check_interval_minutes": 720,
        "enabled": True,
    },
    {
        "name": "UBC Extended Learning",
        "url": "https://extendedlearning.ubc.ca/rss.xml",
        "keywords": [],
        "check_interval_minutes": 720,
        "enabled": True,
    },
    {
        "name": "Arizona Continuing Education",
        "url": "https://ce.arizona.edu/rss.xml",
        "keywords": ["certificate program", "certificate", "bootcamp", "workshop"],
        "check_interval_minutes": 720,
        "enabled": True,
    },
    {
        "name": "George Mason CPE",
        "url": "https://content.sitemasonry.gmu.edu/taxonomy/term/27456/feed",
        "keywords": ["career academy", "industry certificates", "micro-credential", "micro-credentials"],
        "check_interval_minutes": 720,
        "enabled": True,
    },
]


TWITCH_OFFICIAL_TARGETS = [
    {
        "name": "魔兽世界 Twitch 掉宝",
        "url": "https://worldofwarcraft.blizzard.com/en-us/search?keyword=twitch",
        "detection_mode": "content_hash",
        "check_interval_minutes": 60,
        "enabled": True,
    },
    {
        "name": "流放之路 1 Twitch 掉宝",
        "url": "https://www.pathofexile.com/twitchdrops",
        "detection_mode": "content_hash",
        "check_interval_minutes": 60,
        "enabled": True,
    },
    {
        "name": "流放之路 2 Twitch 掉宝",
        "url": "https://pathofexile2.com/twitchdrops",
        "detection_mode": "content_hash",
        "check_interval_minutes": 60,
        "enabled": True,
    },
    {
        "name": "守望先锋新闻",
        "url": "https://overwatch.blizzard.com/en-us/news/",
        "detection_mode": "content_hash",
        "check_interval_minutes": 60,
        "enabled": True,
    },
    {
        "name": "星际战甲 Twitch 搜索",
        "url": "https://www.warframe.com/en/news?search=twitch",
        "detection_mode": "content_hash",
        "check_interval_minutes": 60,
        "enabled": True,
    },
]


def make_vps_placeholders(count: int = 5, start: int = 1) -> list[dict]:
    items = []
    for idx in range(start, start + count):
        items.append({
            "name": f"VPS 页面 {idx}",
            "url": f"https://example.com/vps-{idx}",
            "detection_mode": "content_hash",
            "check_interval_minutes": 10,
            "enabled": False,
        })
    return items


def make_rss_placeholders(count: int = 5, start: int = 1) -> list[dict]:
    items = []
    for idx in range(start, start + count):
        items.append({
            "name": f"RSS 订阅 {idx}",
            "url": f"https://example.com/feed-{idx}.xml",
            "keywords": [],
            "check_interval_minutes": 720,
            "enabled": False,
        })
    return items

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
        "selector_disappears": "选择器消失 → 视为有货  （例如“缺货”标记消失）",
        "selector_appears":    "选择器出现 → 视为有货  （例如“立即购买”按钮出现）",
        "content_hash":        "页面文本变化 → 直接提醒  （无需 CSS 选择器）",
    }

    def __init__(self, parent, existing=None):
        super().__init__(parent)
        self.title("新增 VPS 页面" if not existing else "编辑 VPS 页面")
        self.configure(bg=DARK2)
        self.resizable(False, False)
        self.result = None

        pad = dict(padx=16, pady=6)

        tk.Label(self, text="名称", bg=DARK2, fg=TEXT2,
                 font=("Helvetica", 9)).pack(anchor="w", **pad)
        self.name = tk.Entry(self, bg=DARK3, fg=TEXT, insertbackground=TEXT,
                             relief="flat", bd=0, highlightthickness=1,
                             highlightbackground=BORDER, highlightcolor=ACCENT,
                             font=("Courier", 10), width=50)
        self.name.pack(fill="x", padx=16, ipady=5)

        tk.Label(self, text="链接 URL", bg=DARK2, fg=TEXT2,
                 font=("Helvetica", 9)).pack(anchor="w", **pad)
        self.url = tk.Entry(self, bg=DARK3, fg=TEXT, insertbackground=TEXT,
                            relief="flat", bd=0, highlightthickness=1,
                            highlightbackground=BORDER, highlightcolor=ACCENT,
                            font=("Courier", 10), width=50)
        self.url.pack(fill="x", padx=16, ipady=5)

        tk.Label(self, text="打开链接  （可选：通知里给你点开的公开页面）", bg=DARK2, fg=TEXT2,
                 font=("Helvetica", 9)).pack(anchor="w", **pad)
        self.open_url = tk.Entry(self, bg=DARK3, fg=TEXT, insertbackground=TEXT,
                                 relief="flat", bd=0, highlightthickness=1,
                                 highlightbackground=BORDER, highlightcolor=ACCENT,
                                 font=("Courier", 10), width=50)
        self.open_url.pack(fill="x", padx=16, ipady=5)

        tk.Label(self, text="检测模式", bg=DARK2, fg=TEXT2,
                 font=("Helvetica", 9)).pack(anchor="w", **pad)
        self.mode_var = tk.StringVar(value="selector_disappears")
        for key, label in self.MODES.items():
            tk.Radiobutton(
                self, text=label, variable=self.mode_var, value=key,
                bg=DARK2, fg=TEXT, selectcolor=DARK3,
                activebackground=DARK2, activeforeground=ACCENT,
                font=("Helvetica", 9)
            ).pack(anchor="w", padx=16)

        tk.Label(self, text="CSS 选择器  （如果使用 content_hash，可留空）",
                 bg=DARK2, fg=TEXT2, font=("Helvetica", 9)).pack(anchor="w", **pad)
        self.selector = tk.Entry(self, bg=DARK3, fg=TEXT, insertbackground=TEXT,
                                 relief="flat", bd=0, highlightthickness=1,
                                 highlightbackground=BORDER, highlightcolor=ACCENT,
                                 font=("Courier", 10), width=50)
        self.selector.pack(fill="x", padx=16, ipady=5)
        tk.Label(self, text='  例如：.out-of-stock  或  button[data-action="add-to-cart"]',
                 bg=DARK2, fg=TEXT2, font=("Helvetica", 8)).pack(anchor="w", padx=16)

        tk.Label(self, text="代理  （可选：socks5://user:pass@host:port）",
                 bg=DARK2, fg=TEXT2, font=("Helvetica", 9)).pack(anchor="w", **pad)
        self.proxy = tk.Entry(self, bg=DARK3, fg=TEXT, insertbackground=TEXT,
                              relief="flat", bd=0, highlightthickness=1,
                              highlightbackground=BORDER, highlightcolor=ACCENT,
                              font=("Courier", 10), width=50)
        self.proxy.pack(fill="x", padx=16, ipady=5)

        tk.Label(self, text="单项轮询间隔  （分钟，留空则使用全局设置）",
                 bg=DARK2, fg=TEXT2, font=("Helvetica", 9)).pack(anchor="w", **pad)
        self.interval = tk.Entry(self, bg=DARK3, fg=TEXT, insertbackground=TEXT,
                                 relief="flat", bd=0, highlightthickness=1,
                                 highlightbackground=BORDER, highlightcolor=ACCENT,
                                 font=("Courier", 10), width=12)
        self.interval.pack(anchor="w", padx=16, ipady=5)

        self.enabled_var = tk.BooleanVar(value=True)
        tk.Checkbutton(self, text="启用", variable=self.enabled_var,
                       bg=DARK2, fg=TEXT, selectcolor=DARK3,
                       activebackground=DARK2, activeforeground=ACCENT,
                       font=("Helvetica", 10)).pack(anchor="w", padx=16, pady=4)

        btn_frame = tk.Frame(self, bg=DARK2)
        btn_frame.pack(fill="x", padx=16, pady=12)
        StyledButton(btn_frame, "保存", self._save, color=GREEN).pack(side="left")
        StyledButton(btn_frame, "取消", self.destroy, color=BORDER).pack(side="left", padx=8)

        if existing:
            self.name.insert(0, existing.get("name", ""))
            self.url.insert(0, existing.get("url", ""))
            self.open_url.insert(0, existing.get("open_url", ""))
            self.mode_var.set(existing.get("detection_mode", "selector_disappears"))
            self.selector.insert(0, existing.get("css_selector", ""))
            self.proxy.insert(0, existing.get("proxy", ""))
            # BUG-NEW3 FIX: use is not None so interval=0 is preserved
            if existing.get("check_interval_minutes") is not None:
                self.interval.insert(0, str(existing["check_interval_minutes"]))
            self.enabled_var.set(existing.get("enabled", True))

        self.grab_set()
        self.wait_window()

    def _save(self):
        name = self.name.get().strip()
        url  = self.url.get().strip()
        if not name or not url:
            messagebox.showerror("校验失败", "名称和 URL 不能为空。", parent=self)
            return
        if not url.startswith("http"):
            messagebox.showerror("校验失败", "URL 必须以 http:// 或 https:// 开头。", parent=self)
            return
        interval = self.interval.get().strip()
        self.result = {
            "name": name,
            "url": url,
            "open_url": self.open_url.get().strip(),
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
        self.title("新增 RSS 订阅" if not existing else "编辑 RSS 订阅")
        self.configure(bg=DARK2)
        self.resizable(False, False)
        self.result = None

        pad = dict(padx=16, pady=6)

        tk.Label(self, text="名称", bg=DARK2, fg=TEXT2,
                 font=("Helvetica", 9)).pack(anchor="w", **pad)
        self.name = tk.Entry(self, bg=DARK3, fg=TEXT, insertbackground=TEXT,
                             relief="flat", bd=0, highlightthickness=1,
                             highlightbackground=BORDER, highlightcolor=ACCENT,
                             font=("Courier", 10), width=50)
        self.name.pack(fill="x", padx=16, ipady=5)

        tk.Label(self, text="订阅链接 URL", bg=DARK2, fg=TEXT2,
                 font=("Helvetica", 9)).pack(anchor="w", **pad)
        self.url = tk.Entry(self, bg=DARK3, fg=TEXT, insertbackground=TEXT,
                            relief="flat", bd=0, highlightthickness=1,
                            highlightbackground=BORDER, highlightcolor=ACCENT,
                            font=("Courier", 10), width=50)
        self.url.pack(fill="x", padx=16, ipady=5)

        tk.Label(self, text="关键词过滤  （逗号分隔，留空表示接收全部）",
                 bg=DARK2, fg=TEXT2, font=("Helvetica", 9)).pack(anchor="w", **pad)
        self.keywords = tk.Entry(self, bg=DARK3, fg=TEXT, insertbackground=TEXT,
                                 relief="flat", bd=0, highlightthickness=1,
                                 highlightbackground=BORDER, highlightcolor=ACCENT,
                                 font=("Courier", 10), width=50)
        self.keywords.pack(fill="x", padx=16, ipady=5)
        tk.Label(self, text='  例如：annual, KVM, 512MB, special offer',
                 bg=DARK2, fg=TEXT2, font=("Helvetica", 8)).pack(anchor="w", padx=16)

        tk.Label(self, text="代理  （可选：socks5://user:pass@host:port）",
                 bg=DARK2, fg=TEXT2, font=("Helvetica", 9)).pack(anchor="w", **pad)
        self.proxy = tk.Entry(self, bg=DARK3, fg=TEXT, insertbackground=TEXT,
                              relief="flat", bd=0, highlightthickness=1,
                              highlightbackground=BORDER, highlightcolor=ACCENT,
                              font=("Courier", 10), width=50)
        self.proxy.pack(fill="x", padx=16, ipady=5)

        tk.Label(self, text="单项轮询间隔  （分钟，留空则使用全局设置）",
                 bg=DARK2, fg=TEXT2, font=("Helvetica", 9)).pack(anchor="w", **pad)
        self.interval = tk.Entry(self, bg=DARK3, fg=TEXT, insertbackground=TEXT,
                                 relief="flat", bd=0, highlightthickness=1,
                                 highlightbackground=BORDER, highlightcolor=ACCENT,
                                 font=("Courier", 10), width=12)
        self.interval.pack(anchor="w", padx=16, ipady=5)

        self.enabled_var = tk.BooleanVar(value=True)
        tk.Checkbutton(self, text="启用", variable=self.enabled_var,
                       bg=DARK2, fg=TEXT, selectcolor=DARK3,
                       activebackground=DARK2, activeforeground=ACCENT,
                       font=("Helvetica", 10)).pack(anchor="w", padx=16, pady=4)

        btn_frame = tk.Frame(self, bg=DARK2)
        btn_frame.pack(fill="x", padx=16, pady=12)
        StyledButton(btn_frame, "保存", self._save, color=GREEN).pack(side="left")
        StyledButton(btn_frame, "取消", self.destroy, color=BORDER).pack(side="left", padx=8)

        if existing:
            self.name.insert(0, existing.get("name", ""))
            self.url.insert(0, existing.get("url", ""))
            kws = existing.get("keywords", [])
            if kws:
                self.keywords.insert(0, ", ".join(kws))
            if existing.get("proxy"):
                self.proxy.insert(0, existing.get("proxy", ""))
            # BUG-NEW3 FIX: use is not None so interval=0 is preserved
            if existing.get("check_interval_minutes") is not None:
                self.interval.insert(0, str(existing["check_interval_minutes"]))
            self.enabled_var.set(existing.get("enabled", True))

        self.grab_set()
        self.wait_window()

    def _save(self):
        name = self.name.get().strip()
        url  = self.url.get().strip()
        if not name or not url:
            messagebox.showerror("校验失败", "名称和 URL 不能为空。", parent=self)
            return
        kw_raw = self.keywords.get().strip()
        keywords = [k.strip() for k in kw_raw.split(",") if k.strip()] if kw_raw else []
        interval = self.interval.get().strip()
        proxy = self.proxy.get().strip()
        self.result = {
            "name": name,
            "url": url,
            "keywords": keywords,
            "enabled": self.enabled_var.get(),
        }
        if proxy:
            self.result["proxy"] = proxy
        if interval.isdigit():
            self.result["check_interval_minutes"] = int(interval)
        self.destroy()


# ─── Main Application ─────────────────────────────────────────────────────────

class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("监控配置可视化编辑器")
        self.configure(bg=DARK)
        self.minsize(980, 700)
        self._targets: list[dict] = []
        self._feeds:   list[dict] = []
        self._build_ui()
        self._update_lists()

    # ── Layout ──────────────────────────────────────────────────────────────

    def _build_ui(self):
        # ── Header ──
        header = tk.Frame(self, bg=DARK2, pady=0)
        header.pack(fill="x")
        tk.Label(header, text="⬡  监控配置可视化编辑器",
                 bg=DARK2, fg=ACCENT, font=("Helvetica", 15, "bold"),
                 padx=20, pady=14).pack(side="left")
        tk.Label(header, text="生成 config.yaml → 上传到 VPS 运行",
                 bg=DARK2, fg=TEXT2, font=("Helvetica", 10)).pack(side="left")

        # ── Body ──
        body = tk.Frame(self, bg=DARK)
        body.pack(fill="both", expand=True, padx=20, pady=16)

        # Left column
        left = tk.Frame(body, bg=DARK)
        left.pack(side="left", fill="both", expand=True, padx=(0, 10))

        self._build_quickstart(left)
        self._build_global(left)
        self._build_targets(left)

        # Right column
        right = tk.Frame(body, bg=DARK)
        right.pack(side="left", fill="both", expand=True)

        self._build_rss(right)
        self._build_dashboard(right)
        self._build_preview(right)

        # ── Footer buttons ──
        footer = tk.Frame(self, bg=DARK2)
        footer.pack(fill="x", side="bottom")
        StyledButton(footer, "📥  导出 config.yaml", self._export, color=GREEN).pack(
            side="right", padx=16, pady=10)
        StyledButton(footer, "📂  加载 config.yaml", self._load, color=ORANGE).pack(
            side="right", pady=10)
        StyledButton(footer, "👁  预览 YAML", self._show_preview, color=ACCENT).pack(
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

    def _build_quickstart(self, parent):
        sec = self._section(parent, "🚀  快速配置")

        tk.Label(
            sec,
            text="为你当前的使用场景准备的可视化快捷操作：预填 4 个精准目标、4 个北美大学课程 RSS，另保留 1 个 VPS 占位和 1 个 RSS 占位，再加 5 个 Twitch / 游戏页面。",
            bg=DARK2,
            fg=TEXT2,
            font=("Helvetica", 9),
            wraplength=440,
            justify="left",
        ).pack(anchor="w", pady=(0, 10))

        row1 = tk.Frame(sec, bg=DARK2)
        row1.pack(fill="x", pady=(0, 8))
        StyledButton(row1, "载入精准模板", self._load_my_template, color=GREEN).pack(side="left")
        StyledButton(row1, "加入 Twitch 套餐", self._add_twitch_bundle, color=ACCENT).pack(side="left", padx=8)

        row2 = tk.Frame(sec, bg=DARK2)
        row2.pack(fill="x", pady=(0, 8))
        StyledButton(row2, "新增 5 个 VPS 占位", self._add_vps_slots, color=ACCENT).pack(side="left")
        StyledButton(row2, "新增 5 个 RSS 占位", self._add_rss_slots, color=ACCENT).pack(side="left", padx=8)
        StyledButton(row2, "清空页面与订阅", self._clear_targets_and_feeds, color=RED).pack(side="left")

        self.status_var = tk.StringVar(value="建议先点击模板按钮，再双击列表中的条目修改名称和 URL。")
        tk.Label(
            sec,
            textvariable=self.status_var,
            bg=DARK2,
            fg=ORANGE,
            font=("Helvetica", 9),
            wraplength=440,
            justify="left",
        ).pack(anchor="w", pady=(4, 0))

    def _build_dashboard(self, parent):
        sec = self._section(parent, "📊  总览")

        cards = tk.Frame(sec, bg=DARK2)
        cards.pack(fill="x")

        self.apprise_count_var = tk.StringVar(value="0")
        self.target_count_var = tk.StringVar(value="0 / 0")
        self.feed_count_var = tk.StringVar(value="0 / 0")

        def card(parent_frame, title, value_var, color):
            frame = tk.Frame(parent_frame, bg=DARK3, highlightthickness=1, highlightbackground=BORDER)
            frame.pack(side="left", fill="both", expand=True, padx=(0, 8))
            tk.Label(frame, text=title, bg=DARK3, fg=TEXT2, font=("Helvetica", 9)).pack(anchor="w", padx=10, pady=(8, 2))
            tk.Label(frame, textvariable=value_var, bg=DARK3, fg=color, font=("Helvetica", 16, "bold")).pack(anchor="w", padx=10, pady=(0, 8))
            return frame

        card(cards, "通知地址", self.apprise_count_var, ACCENT)
        card(cards, "已启用页面", self.target_count_var, GREEN)
        card(cards, "已启用 RSS", self.feed_count_var, ORANGE)

        self.hint_var = tk.StringVar(value="占位行可以暂时保留；导出前再替换成你自己的真实链接即可。")
        tk.Label(
            sec,
            textvariable=self.hint_var,
            bg=DARK2,
            fg=TEXT2,
            font=("Helvetica", 9),
            wraplength=460,
            justify="left",
        ).pack(anchor="w", pady=(10, 0))

    # ── Global Settings ──────────────────────────────────────────────────────

    def _build_global(self, parent):
        inner = self._section(parent, "⚙  全局设置")

        # Apprise URLs
        tk.Label(inner, text="通知地址  （每行一个，支持 tgram://、mailto://、slack:// 等）",
                 bg=DARK2, fg=TEXT2, font=("Helvetica", 9)).pack(anchor="w")
        self.apprise_text = tk.Text(inner, height=4, bg=DARK3, fg=TEXT,
                                    insertbackground=TEXT, relief="flat", bd=0,
                                    highlightthickness=1, highlightbackground=BORDER,
                                    highlightcolor=ACCENT, font=("Courier", 10))
        self.apprise_text.pack(fill="x", ipady=4, pady=(0, 8))
        self.apprise_text.bind("<KeyRelease>", lambda _: self._refresh_dashboard())
        tk.Label(inner, text="  例如：tgram://BotToken/ChatID   |   mailto://user:pass@gmail.com",
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
            e.bind("<KeyRelease>", lambda _: self._refresh_dashboard())
            return e

        self.interval_entry   = num_entry("默认轮询间隔（分钟）", 30)
        self.heartbeat_entry  = num_entry("心跳间隔（小时）", 24)
        self.timeout_entry    = num_entry("请求超时（秒）", 30)
        self.delay_min_entry  = num_entry("重试延迟最小值（秒）", 1.0, 6)
        self.delay_max_entry  = num_entry("重试延迟最大值（秒）", 3.0, 6)

    # ── VPS Targets ──────────────────────────────────────────────────────────

    def _build_targets(self, parent):
        sec = self._section(parent, "🖥  页面监控目标")

        # Toolbar
        tb = tk.Frame(sec, bg=DARK2)
        tb.pack(fill="x", pady=(0, 8))
        StyledButton(tb, "+ 新增页面", self._add_target, color=ACCENT).pack(side="left")
        StyledButton(tb, "✎ 编辑", self._edit_target, color=DARK3).pack(side="left", padx=6)
        StyledButton(tb, "✕ 删除", self._del_target, color=RED).pack(side="left")

        # List
        cols = ("name", "mode", "interval", "enabled")
        self.target_tree = ttk.Treeview(sec, columns=cols, show="headings",
                                        height=6, style="Dark.Treeview")
        self.target_tree.heading("name",     text="名称")
        self.target_tree.heading("mode",     text="模式")
        self.target_tree.heading("interval", text="间隔")
        self.target_tree.heading("enabled",  text="启用")
        self.target_tree.column("name",     width=180)
        self.target_tree.column("mode",     width=160)
        self.target_tree.column("interval", width=70)
        self.target_tree.column("enabled",  width=40)
        self.target_tree.pack(fill="both", expand=True)
        self.target_tree.bind("<Double-1>", lambda _: self._edit_target())
        self._style_tree()

    # ── RSS Feeds ────────────────────────────────────────────────────────────

    def _build_rss(self, parent):
        sec = self._section(parent, "📡  RSS 订阅")

        tb = tk.Frame(sec, bg=DARK2)
        tb.pack(fill="x", pady=(0, 8))
        StyledButton(tb, "+ 新增 RSS", self._add_feed, color=ACCENT).pack(side="left")
        StyledButton(tb, "✎ 编辑",    self._edit_feed, color=DARK3).pack(side="left", padx=6)
        StyledButton(tb, "✕ 删除",  self._del_feed,  color=RED).pack(side="left")

        cols = ("name", "keywords", "interval", "enabled")
        self.feed_tree = ttk.Treeview(sec, columns=cols, show="headings",
                                      height=6, style="Dark.Treeview")
        self.feed_tree.heading("name",     text="名称")
        self.feed_tree.heading("keywords", text="关键词")
        self.feed_tree.heading("interval", text="间隔")
        self.feed_tree.heading("enabled",  text="启用")
        self.feed_tree.column("name",     width=150)
        self.feed_tree.column("keywords", width=200)
        self.feed_tree.column("interval", width=70)
        self.feed_tree.column("enabled",  width=40)
        self.feed_tree.pack(fill="both", expand=True)
        self.feed_tree.bind("<Double-1>", lambda _: self._edit_feed())

    # ── YAML Preview ─────────────────────────────────────────────────────────

    def _build_preview(self, parent):
        sec = self._section(parent, "📄  YAML 预览")
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
        if messagebox.askyesno("确认删除", f"要删除页面监控“{name}”吗？", parent=self):
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
        if messagebox.askyesno("确认删除", f"要删除 RSS 订阅“{name}”吗？", parent=self):
            self._feeds.pop(idx)
            self._update_lists()

    def _update_lists(self):
        # Targets
        for item in self.target_tree.get_children():
            self.target_tree.delete(item)
        for t in self._targets:
            mode = t.get("detection_mode", "selector_disappears")
            short_mode = {"selector_disappears": "消失触发", "selector_appears": "出现触发",
                          "content_hash": "文本变化"}.get(mode, mode)
            _iv = t.get("check_interval_minutes")
            interval = str(_iv) if _iv is not None else "全局"
            enabled = "是" if t.get("enabled", True) else "否"
            self.target_tree.insert("", "end", values=(
                t.get("name", ""), short_mode, interval, enabled
            ))

        # Feeds
        for item in self.feed_tree.get_children():
            self.feed_tree.delete(item)
        for f in self._feeds:
            kws = ", ".join(f.get("keywords", [])) or "全部"
            _iv = f.get("check_interval_minutes")
            interval = str(_iv) if _iv is not None else "全局"
            enabled = "是" if f.get("enabled", True) else "否"
            self.feed_tree.insert("", "end", values=(
                f.get("name", ""), kws, interval, enabled
            ))

        self._refresh_dashboard()

    def _refresh_dashboard(self):
        apprise_count = len([u for u in self.apprise_text.get("1.0", "end").splitlines() if u.strip()])
        enabled_targets = sum(1 for t in self._targets if t.get("enabled", True))
        enabled_feeds = sum(1 for f in self._feeds if f.get("enabled", True))

        self.apprise_count_var.set(str(apprise_count))
        self.target_count_var.set(f"{enabled_targets} / {len(self._targets)}")
        self.feed_count_var.set(f"{enabled_feeds} / {len(self._feeds)}")

        placeholders_left = sum(1 for t in self._targets if t.get("url", "").startswith("https://example.com/"))
        placeholders_left += sum(1 for f in self._feeds if f.get("url", "").startswith("https://example.com/"))
        if placeholders_left:
            self.hint_var.set(f"还有 {placeholders_left} 条占位记录需要替换成你的真实链接，完成后再导出。")
        elif not apprise_count:
            self.hint_var.set("还没有填写通知地址。现在也能导出，但运行后不会发送提醒。")
        else:
            self.hint_var.set("当前配置看起来已经可以使用。你可以继续预览 YAML，或直接导出。")

        self._show_preview()

    def _append_unique(self, current: list[dict], new_items: list[dict], key_fields: tuple[str, ...]) -> int:
        existing = {tuple(item.get(field, "") for field in key_fields) for item in current}
        added = 0
        for item in new_items:
            key = tuple(item.get(field, "") for field in key_fields)
            if key in existing:
                continue
            current.append(json.loads(json.dumps(item)))
            existing.add(key)
            added += 1
        return added

    def _next_placeholder_index(self, items: list[dict], prefix: str) -> int:
        found = 0
        pattern = re.compile(rf"^{re.escape(prefix)}\s+(\d+)$")
        for item in items:
            match = pattern.match(str(item.get("name", "")).strip())
            if match:
                found = max(found, int(match.group(1)))
        return found + 1

    def _set_default_globals(self):
        pairs = (
            (self.interval_entry, "30"),
            (self.heartbeat_entry, "24"),
            (self.timeout_entry, "30"),
            (self.delay_min_entry, "1.0"),
            (self.delay_max_entry, "3.0"),
        )
        for entry, value in pairs:
            entry.delete(0, "end")
            entry.insert(0, value)

    def _load_my_template(self):
        if self._targets or self._feeds:
            if not messagebox.askyesno(
                "替换当前配置？",
                "这会用“4 个精准目标 + 1 个 VPS 占位 + 4 个大学 RSS + 1 个 RSS 占位 + 5 个 Twitch 页面”的初始模板替换当前的页面和订阅列表。\n\n继续吗？",
                parent=self,
            ):
                return

        self._set_default_globals()
        self._targets = (
            json.loads(json.dumps(PRESET_PRECISE_TARGETS))
            + make_vps_placeholders(1)
            + json.loads(json.dumps(TWITCH_OFFICIAL_TARGETS))
        )
        self._feeds = json.loads(json.dumps(PRESET_UNIVERSITY_FEEDS)) + make_rss_placeholders(1)
        self.status_var.set(
            "模板已载入。前 4 个页面和前 4 个 RSS 是按你需求预填的，另外保留了 1 个 VPS 占位和 1 个 RSS 占位；Twitch / 游戏页面也已经预填好了。"
        )
        self._update_lists()

    def _add_twitch_bundle(self):
        added = self._append_unique(self._targets, TWITCH_OFFICIAL_TARGETS, ("name", "url"))
        self.status_var.set(f"已新增 {added} 个 Twitch / 游戏官方页面。")
        self._update_lists()

    def _add_vps_slots(self):
        start = self._next_placeholder_index(self._targets, "VPS 页面")
        added = self._append_unique(self._targets, make_vps_placeholders(5, start), ("name", "url"))
        self.status_var.set(f"已新增 {added} 个 VPS 占位页面。导出前请把 example.com 链接改成你的真实链接。")
        self._update_lists()

    def _add_rss_slots(self):
        start = self._next_placeholder_index(self._feeds, "RSS 订阅")
        added = self._append_unique(self._feeds, make_rss_placeholders(5, start), ("name", "url"))
        self.status_var.set(f"已新增 {added} 个 RSS 占位订阅。导出前请把 example.com 链接改成你的真实链接。")
        self._update_lists()

    def _clear_targets_and_feeds(self):
        if not self._targets and not self._feeds:
            self.status_var.set("当前没有可清空的页面或订阅。")
            return
        if not messagebox.askyesno(
            "确认清空",
            "要清空全部页面和 RSS 订阅吗？通知地址和全局设置会保留。",
            parent=self,
        ):
            return
        self._targets = []
        self._feeds = []
        self.status_var.set("页面和 RSS 订阅已清空。")
        self._update_lists()

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
                "提醒",
                "你还没有设置通知地址。程序虽然会抓取，但不会发提醒。\n\n仍然继续导出吗？",
                parent=self
            ):
                return
        path = filedialog.asksaveasfilename(
            defaultextension=".yaml",
            filetypes=[("YAML 文件", "*.yaml *.yml"), ("所有文件", "*.*")],
            initialfile="config.yaml",
            title="导出 config.yaml"
        )
        if not path:
            return
        try:
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(build_yaml(cfg))
            messagebox.showinfo(
                "导出完成",
                f"已保存到：\n{path}\n\n把这个文件上传到 VPS，并放到和 worker.py 相同的目录即可。",
                parent=self
            )
        except Exception as exc:
            messagebox.showerror("错误", str(exc), parent=self)

    def _load(self):
        """Load an existing config.yaml back into the GUI."""
        path = filedialog.askopenfilename(
            filetypes=[("YAML 文件", "*.yaml *.yml"), ("所有文件", "*.*")],
            title="加载 config.yaml"
        )
        if not path:
            return
        try:
            # Minimal YAML parser for our known structure
            self._load_yaml_file(path)
        except Exception as exc:
            messagebox.showerror("加载失败", str(exc), parent=self)

    def _load_yaml_file(self, path: str):
        """Very basic YAML loader for our config format (no PyYAML needed)."""
        with open(path, encoding="utf-8") as fh:
            content = fh.read()

        try:
            import importlib
            yaml_mod = importlib.import_module("yaml")
            data = yaml_mod.safe_load(content)
        except ImportError:
            # Minimal manual parse — enough for round-trip of our own output
            data = self._simple_yaml_parse(content)

        if not isinstance(data, dict):
            raise ValueError("配置文件最外层必须是映射结构。")

        g = data.get("global", {}) or {}
        if not isinstance(g, dict):
            g = {}
        urls = g.get("apprise_urls", [])
        if isinstance(urls, str):
            urls = [urls]
        elif not isinstance(urls, list):
            urls = []
        urls = [str(u) for u in urls if str(u).strip()]
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
        if not isinstance(self._targets, list):
            self._targets = []
        self._feeds = data.get("rss_feeds", []) or []
        if not isinstance(self._feeds, list):
            self._feeds = []
        self._update_lists()
        messagebox.showinfo("加载完成", f"已加载 {len(self._targets)} 个页面目标和 "
                            f"{len(self._feeds)} 个 RSS 订阅。", parent=self)


    def _simple_yaml_parse(self, text: str) -> dict:
        """Fallback parser for the limited YAML produced by build_yaml()."""
        def strip_comment(line: str) -> str:
            out = []
            in_quote = False
            escape = False
            for ch in line:
                if escape:
                    out.append(ch)
                    escape = False
                    continue
                if ch == '\\':
                    out.append(ch)
                    escape = True
                    continue
                if ch == '"':
                    out.append(ch)
                    in_quote = not in_quote
                    continue
                if ch == '#' and not in_quote:
                    break
                out.append(ch)
            return ''.join(out).rstrip()

        def parse_scalar(value: str):
            value = value.strip()
            if value == "":
                return ""
            if value == "[]":
                return []
            low = value.lower()
            if low == "true":
                return True
            if low == "false":
                return False
            if value.startswith('"') and value.endswith('"') and len(value) >= 2:
                inner = value[1:-1]
                inner = inner.replace('\\n', '\n').replace('\\r', '\r').replace('\\t', '\t')
                inner = inner.replace('\\"', '"').replace('\\\\', '\\')
                return inner
            if re.fullmatch(r"-?\d+", value):
                return int(value)
            if re.fullmatch(r"-?(?:\d+\.\d+|\d+\.|\.\d+)", value):
                return float(value)
            return value

        root = {"global": {}, "targets": [], "rss_feeds": []}
        section = None
        current_item = None
        current_list_key = None

        for raw_line in text.splitlines():
            line = strip_comment(raw_line)
            if not line.strip():
                continue

            indent = len(line) - len(line.lstrip(' '))
            stripped = line.strip()

            if indent == 0 and stripped.endswith(':'):
                key = stripped[:-1]
                if key not in root:
                    raise ValueError(f"不支持的顶层键：{key}")
                section = key
                current_item = None
                current_list_key = None
                continue

            if section is None:
                continue

            if section == 'global':
                if indent == 4 and stripped.startswith('- '):
                    if not current_list_key:
                        raise ValueError(f"global 段中出现了意外的列表项：{raw_line}")
                    root['global'].setdefault(current_list_key, []).append(parse_scalar(stripped[2:].strip()))
                    continue
                if indent != 2:
                    raise ValueError(f"global 段缩进不正确：{raw_line}")
                key, sep, value = stripped.partition(':')
                if not sep:
                    raise ValueError(f"global 段格式无效：{raw_line}")
                value = value.strip()
                if value == '':
                    current_list_key = key
                    root['global'][key] = []
                else:
                    current_list_key = None
                    root['global'][key] = parse_scalar(value)
                continue

            if section in ('targets', 'rss_feeds'):
                if indent == 2 and stripped == '[]':
                    current_item = None
                    current_list_key = None
                    continue
                if indent == 2 and stripped.startswith('- '):
                    current_item = {}
                    root[section].append(current_item)
                    current_list_key = None
                    remainder = stripped[2:].strip()
                    if remainder:
                        key, sep, value = remainder.partition(':')
                        if not sep:
                            raise ValueError(f"列表项格式无效：{raw_line}")
                        current_item[key.strip()] = parse_scalar(value.strip())
                    continue
                if current_item is None:
                    raise ValueError(f"{section} 段中存在未归属到列表项的属性：{raw_line}")
                if indent == 4:
                    key, sep, value = stripped.partition(':')
                    if not sep:
                        raise ValueError(f"映射行格式无效：{raw_line}")
                    key = key.strip()
                    value = value.strip()
                    if value == '':
                        current_item[key] = []
                        current_list_key = key
                    else:
                        current_item[key] = parse_scalar(value)
                        current_list_key = None
                    continue
                if indent == 6 and stripped.startswith('- '):
                    if not current_list_key:
                        raise ValueError(f"出现了意外的嵌套列表项：{raw_line}")
                    current_item.setdefault(current_list_key, []).append(parse_scalar(stripped[2:].strip()))
                    continue
                raise ValueError(f"不支持的 YAML 结构：{raw_line}")

        return root



# ─── Entry Point ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
    app = App()
    app.mainloop()
