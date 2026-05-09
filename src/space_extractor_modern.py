# space_extractor_modern.py
import os
import sys
import hashlib
import threading
import shutil
import subprocess
import tempfile
import time
from queue import Queue
import heapq
from pathlib import Path
from collections import defaultdict, Counter

import customtkinter as ctk
from tkinter import filedialog, messagebox
from tkinter.scrolledtext import ScrolledText

import matplotlib.pyplot as plt
from matplotlib.figure import Figure
from matplotlib.backends.backend_agg import FigureCanvasAgg as FigureCanvas

import psutil
import humanize
from send2trash import send2trash  # safe delete to Recycle Bin

# ----------------------------
# Appearance
# ----------------------------
ctk.set_appearance_mode("dark")
ctk.set_default_color_theme("blue")

APP_WIDTH = 1100
APP_HEIGHT = 700

# --------- Output font (bigger, readable) ----------
OUTPUT_FONT = ("Consolas", 14)  # bigger, readable monospace font

# ----------------------------
# Helpers (filesystem analysis)
# ----------------------------
def safe_getsize(path):
    try:
        return os.path.getsize(path)
    except Exception:
        return 0

def iter_all_files(folder):
    """Yield absolute file paths under folder, skipping reparse/mount points."""
    for dirpath, dirnames, filenames in os.walk(folder, topdown=True):
        # avoid following mounts/junctions / symlinks
        cleaned = []
        for d in dirnames:
            full = os.path.join(dirpath, d)
            try:
                if os.path.islink(full) or os.path.ismount(full):
                    continue
            except Exception:
                continue
            cleaned.append(d)
        dirnames[:] = cleaned
        for f in filenames:
            yield os.path.join(dirpath, f)

def md5_hash(file_path, chunk_size=8192):
    h = hashlib.md5()
    try:
        with open(file_path, 'rb') as f:
            while True:
                chunk = f.read(chunk_size)
                if not chunk:
                    break
                h.update(chunk)
        return h.hexdigest()
    except Exception:
        return None

# ----------------------------
# Faster, streaming folder stats with cancellation and progress callback
# ----------------------------
def _make_ext_summary(ext_counts, ext_sizes):
    items = []
    for ext, cnt in ext_counts.items():
        items.append((ext, cnt, ext_sizes.get(ext, 0)))
    items.sort(key=lambda x: x[2], reverse=True)
    return items

def _heap_to_list(h):
    return sorted(h, key=lambda x: x[0], reverse=True)

def collect_folder_stats_streaming(folder, progress_callback=None, total_estimate=None,
                                   update_every=200, top_n=25, cancel_event=None):
    """
    Streaming scan of `folder`. Returns (total_files, total_size, ext_summary, top_files)
    - progress_callback(processed_count, total_estimate_or_None, elapsed_seconds) is called occasionally.
    - cancel_event is optional threading.Event(); if set, scanning stops early and returns partial results.
    """
    ext_counts = defaultdict(int)
    ext_sizes = defaultdict(int)
    total_files = 0
    total_size = 0
    top_heap = []

    processed = 0
    start = time.time()

    for dirpath, dirnames, filenames in os.walk(folder, topdown=True):
        # Prune symlinks and mounts
        new_dirs = []
        for d in dirnames:
            full = os.path.join(dirpath, d)
            try:
                if os.path.islink(full) or os.path.ismount(full):
                    continue
            except Exception:
                continue
            new_dirs.append(d)
        dirnames[:] = new_dirs

        for fname in filenames:
            if cancel_event and cancel_event.is_set():
                elapsed = time.time() - start
                return total_files, total_size, _make_ext_summary(ext_counts, ext_sizes), _heap_to_list(top_heap)

            fp = os.path.join(dirpath, fname)
            try:
                sz = os.path.getsize(fp)
            except (PermissionError, FileNotFoundError, OSError):
                sz = 0

            total_files += 1
            total_size += sz
            ext = Path(fp).suffix.lower() or "<no-ext>"
            ext_counts[ext] += 1
            ext_sizes[ext] += sz

            if len(top_heap) < top_n:
                heapq.heappush(top_heap, (sz, fp))
            else:
                heapq.heappushpop(top_heap, (sz, fp))

            processed += 1
            if progress_callback and (processed % update_every == 0):
                elapsed = time.time() - start
                try:
                    progress_callback(processed, total_estimate, elapsed)
                except Exception:
                    pass

    # final update
    if progress_callback:
        try:
            progress_callback(processed, total_estimate, time.time() - start)
        except Exception:
            pass

    ext_summary = _make_ext_summary(ext_counts, ext_sizes)
    top_files = _heap_to_list(top_heap)
    return total_files, total_size, ext_summary, top_files

def quick_count_files(path):
    """Optional fast count of files (walk-only, no stat on each file)."""
    count = 0
    for _, _, filenames in os.walk(path):
        count += len(filenames)
    return count

# ----------------------------
# Cache helper (deduped)
# ----------------------------
def get_common_cache_paths():
    paths = []
    localapp = os.environ.get('LOCALAPPDATA', '')
    userprofile = os.environ.get('USERPROFILE', '')
    windir = os.environ.get('WINDIR', '')
    appdata = os.environ.get('APPDATA', '')
    candidates = [
        os.path.join(userprofile, 'AppData', 'Local', 'Temp') if userprofile else '',
        os.path.join(localapp, 'Temp') if localapp else '',
        os.path.join(windir, 'Temp') if windir else '',
        os.path.join(localapp, 'Microsoft', 'Edge', 'User Data', 'Default', 'Cache') if localapp else '',
        os.path.join(localapp, 'Google', 'Chrome', 'User Data', 'Default', 'Cache') if localapp else '',
        os.path.join(appdata, 'Code', 'Cache') if appdata else '',
        os.path.join(localapp, 'npm-cache') if localapp else '',
        os.path.join(localapp, 'Temp', 'node-compile-cache') if localapp else '',
    ]
    # dedupe & exist
    unique = []
    seen = set()
    for p in candidates:
        if not p:
            continue
        norm = os.path.normpath(p)
        if norm in seen:
            continue
        if os.path.exists(norm):
            seen.add(norm)
            unique.append(norm)
    return unique

def get_cache_summary(top_n=10):
    """Return dict of path -> {size, files, folders, top: [(size,path)...]}"""
    result = {}
    for p in get_common_cache_paths():
        total_size = 0
        files_count = 0
        folders = set()
        top_heap = []
        for dirpath, dirnames, filenames in os.walk(p, topdown=True):
            dirnames[:] = [d for d in dirnames if not os.path.islink(os.path.join(dirpath, d)) and not os.path.ismount(os.path.join(dirpath, d))]
            folders.add(dirpath)
            for f in filenames:
                fp = os.path.join(dirpath, f)
                try:
                    sz = os.path.getsize(fp)
                except Exception:
                    continue
                total_size += sz
                files_count += 1
                if len(top_heap) < top_n:
                    heapq.heappush(top_heap, (sz, fp))
                else:
                    heapq.heappushpop(top_heap, (sz, fp))
        top_list = sorted(top_heap, key=lambda x: x[0], reverse=True)
        result[p] = {'size': total_size, 'files': files_count, 'folders': len(folders), 'top': top_list}
    return result

# ----------------------------
# Non-blocking plotting helper
# ----------------------------
def show_figure_nonblocking(fig):
    """Render a Matplotlib Figure to a temporary PNG and open with OS default viewer."""
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".png")
    tmpname = tmp.name
    tmp.close()
    try:
        canvas = FigureCanvas(fig)
        canvas.print_figure(tmpname, dpi=150)
    except Exception:
        try:
            fig.savefig(tmpname)
        except Exception:
            pass

    try:
        if sys.platform.startswith('win'):
            os.startfile(tmpname)
        else:
            opener = 'open' if sys.platform == 'darwin' else 'xdg-open'
            subprocess.Popen([opener, tmpname], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        # if opening fails, simply leave the file and do nothing
        pass

# ----------------------------
# GUI
# ----------------------------
root = ctk.CTk()
root.title("Smart Space Extractor — Modern")
root.geometry(f"{APP_WIDTH}x{APP_HEIGHT}")
root.minsize(900, 620)

# main layout frames
sidebar = ctk.CTkFrame(root, width=220, corner_radius=8)
sidebar.pack(side='left', fill='y', padx=12, pady=12)

content = ctk.CTkFrame(root, corner_radius=8)
content.pack(side='right', expand=True, fill='both', padx=12, pady=12)

# Sidebar content
title_lbl = ctk.CTkLabel(sidebar, text="Smart Space", font=ctk.CTkFont(size=18, weight="bold"))
title_lbl.pack(pady=(8,12))

# Buttons in sidebar
def set_active(btn):
    # visuals: highlight active - simple approach
    for child in sidebar.winfo_children():
        if isinstance(child, ctk.CTkButton):
            try:
                child.configure(fg_color=None)
            except Exception:
                pass
    try:
        btn.configure(fg_color="#1b6bff")
    except Exception:
        pass

btn_dashboard = ctk.CTkButton(sidebar, text="🏠 Dashboard", width=200, command=lambda: show_frame('dashboard'))
btn_dashboard.pack(pady=6)
btn_visual = ctk.CTkButton(sidebar, text="📂 Folder Visualizer", width=200, command=lambda: show_frame('visualizer'))
btn_visual.pack(pady=6)
btn_cache = ctk.CTkButton(sidebar, text="🧹 Cache Cleaner", width=200, command=lambda: show_frame('cache'))
btn_cache.pack(pady=6)
btn_drive = ctk.CTkButton(sidebar, text="💽 Drive Info", width=200, command=lambda: show_frame('drive'))
btn_drive.pack(pady=6)

exit_btn = ctk.CTkButton(sidebar, text="Exit", width=200, fg_color="#ff4444", hover_color="#cc3333", command=root.quit)
exit_btn.pack(side='bottom', pady=8)

# Progress area (top of content)
top_bar = ctk.CTkFrame(content, height=60)
top_bar.pack(fill='x', padx=12, pady=(8,12))

status_var = ctk.StringVar(value="Ready")
status_lbl = ctk.CTkLabel(top_bar, textvariable=status_var, anchor='w')
status_lbl.pack(side='left', padx=12)

progressbar = ctk.CTkProgressBar(top_bar, width=350)
progressbar.set(0)
progressbar.pack(side='right', padx=12)

# Frames for different pages
frames = {}
for name in ('dashboard', 'visualizer', 'cache', 'drive'):
    frame = ctk.CTkFrame(content, corner_radius=6)
    frames[name] = frame
    frame.pack(fill='both', expand=True)
    frame.pack_forget()

def show_frame(name):
    for n, f in frames.items():
        f.pack_forget()
    frames[name].pack(fill='both', expand=True)
    # highlight sidebar button
    mapping = {
        'dashboard': btn_dashboard,
        'visualizer': btn_visual,
        'cache': btn_cache,
        'drive': btn_drive
    }
    try:
        set_active(mapping[name])
    except Exception:
        pass

class AccordionCard(ctk.CTkFrame):
    def __init__(self, master, title, default_open=False, **kwargs):
        super().__init__(master, fg_color="transparent", **kwargs)
        self.is_open = default_open
        self.btn = ctk.CTkButton(self, text=("▼ " if self.is_open else "▶ ") + title, 
                                 anchor="w", font=ctk.CTkFont(weight="bold"), 
                                 fg_color="#2b2b2b", hover_color="#3b3b3b",
                                 command=self.toggle)
        self.btn.pack(fill='x', pady=(0, 2))
        self.content_frame = ctk.CTkFrame(self)
        if self.is_open:
            self.content_frame.pack(fill='both', expand=True, padx=4, pady=4)
            
    def toggle(self):
        self.is_open = not self.is_open
        if self.is_open:
            self.btn.configure(text="▼ " + self.btn.cget("text")[2:])
            self.content_frame.pack(fill='both', expand=True, padx=4, pady=4)
        else:
            self.btn.configure(text="▶ " + self.btn.cget("text")[2:])
            self.content_frame.pack_forget()

# ----------------------------
# Dashboard frame
# ----------------------------
dash = frames['dashboard']
dash.pack(padx=12, pady=12)

lbl = ctk.CTkLabel(dash, text="Dashboard — Quick Folder Scan", font=ctk.CTkFont(size=22, weight="bold"))
lbl.pack(anchor='w', pady=(8,12))

dash_select_frame = ctk.CTkFrame(dash, fg_color="transparent")
dash_select_frame.pack(fill='x', pady=(4,12))

dash_path_var = ctk.StringVar()
dash_entry = ctk.CTkEntry(dash_select_frame, textvariable=dash_path_var, width=540, height=36, font=ctk.CTkFont(size=14))
dash_entry.pack(side='left', padx=(0,12))

# cancel event for dashboard scan
dash_cancel_event = None
dash_lock = threading.Lock()

def choose_dash_folder():
    p = filedialog.askdirectory()
    if p:
        dash_path_var.set(p)

dash_browse_btn = ctk.CTkButton(dash_select_frame, text="Browse", width=100, height=36, font=ctk.CTkFont(weight="bold"), command=choose_dash_folder)
dash_browse_btn.pack(side='left', padx=6)

def dash_scan_button():
    p = dash_path_var.get()
    threading.Thread(target=dashboard_scan, args=(p,), daemon=True).start()

dash_scan_btn = ctk.CTkButton(dash_select_frame, text="Start Scan", width=120, height=36, fg_color="#28a745", hover_color="#218838", font=ctk.CTkFont(weight="bold"), command=dash_scan_button)
dash_scan_btn.pack(side='left', padx=6)

dash_cancel_btn = ctk.CTkButton(dash_select_frame, text="Cancel", width=100, height=36, fg_color="#dc3545", hover_color="#c82333", font=ctk.CTkFont(weight="bold"), command=lambda: cancel_scan('dashboard'))
dash_cancel_btn.pack(side='left', padx=6)

dash_chart_btn = ctk.CTkButton(dash_select_frame, text="📊 View Chart", width=120, height=36, font=ctk.CTkFont(weight="bold"), state="disabled", command=lambda: show_dash_chart())
dash_chart_btn.pack(side='right', padx=6)

dash_output_scroll = ctk.CTkScrollableFrame(dash, fg_color="transparent")
dash_output_scroll.pack(fill='both', expand=True, pady=(8,0))

# ----------------------------
# Folder Visualizer frame
# ----------------------------
viz = frames['visualizer']
viz.pack(padx=12, pady=12)

v_lbl = ctk.CTkLabel(viz, text="Folder Visualizer & Deep Analysis", font=ctk.CTkFont(size=22, weight="bold"))
v_lbl.pack(anchor='w', pady=(8,12))

viz_select_frame = ctk.CTkFrame(viz, fg_color="transparent")
viz_select_frame.pack(fill='x', pady=(4,12))

viz_path_var = ctk.StringVar()
viz_entry = ctk.CTkEntry(viz_select_frame, textvariable=viz_path_var, width=540, height=36, font=ctk.CTkFont(size=14))
viz_entry.pack(side='left', padx=(0,12))

# cancel event for viz scan
viz_cancel_event = None
viz_lock = threading.Lock()

def choose_viz_folder():
    p = filedialog.askdirectory()
    if p:
        viz_path_var.set(p)

viz_browse_btn = ctk.CTkButton(viz_select_frame, text="Browse", width=100, height=36, font=ctk.CTkFont(weight="bold"), command=choose_viz_folder)
viz_browse_btn.pack(side='left', padx=6)

def viz_scan_button():
    p = viz_path_var.get()
    threading.Thread(target=folder_visualize, args=(p,), daemon=True).start()

viz_scan_btn = ctk.CTkButton(viz_select_frame, text="Analyze", width=120, height=36, fg_color="#28a745", hover_color="#218838", font=ctk.CTkFont(weight="bold"), command=viz_scan_button)
viz_scan_btn.pack(side='left', padx=6)

viz_cancel_btn = ctk.CTkButton(viz_select_frame, text="Cancel", width=100, height=36, fg_color="#dc3545", hover_color="#c82333", font=ctk.CTkFont(weight="bold"), command=lambda: cancel_scan('visualizer'))
viz_cancel_btn.pack(side='left', padx=6)

viz_chart_btn = ctk.CTkButton(viz_select_frame, text="🥧 View Pie Chart", width=140, height=36, font=ctk.CTkFont(weight="bold"), state="disabled", command=lambda: show_viz_chart())
viz_chart_btn.pack(side='right', padx=6)

viz_output_scroll = ctk.CTkScrollableFrame(viz, fg_color="transparent")
viz_output_scroll.pack(fill='both', expand=True, pady=(8,0))

# ----------------------------
# Cache frame
# ----------------------------
cachef = frames['cache']
cachef.pack(padx=12, pady=12)

c_lbl = ctk.CTkLabel(cachef, text="Cache Cleaner", font=ctk.CTkFont(size=18, weight="bold"))
c_lbl.pack(anchor='w', pady=(8, 12))

cache_btn_frame = ctk.CTkFrame(cachef)
cache_btn_frame.pack(fill='x', pady=(0, 8))

cache_scan_btn = ctk.CTkButton(cache_btn_frame, text="🔍 Scan Default Caches", command=lambda: threading.Thread(target=cache_scan_and_report, daemon=True).start())
cache_scan_btn.pack(side='left', padx=8, pady=8)

def choose_custom_cache():
    p = filedialog.askdirectory()
    if p:
        threading.Thread(target=cache_scan_custom, args=(p,), daemon=True).start()

cache_custom_btn = ctk.CTkButton(cache_btn_frame, text="📂 Add Custom Folder", command=choose_custom_cache)
cache_custom_btn.pack(side='left', padx=8, pady=8)

def toggle_all_caches(select=True):
    for var in cache_checkbox_vars.values():
        var.set(1 if select else 0)

cache_deselect_all_btn = ctk.CTkButton(cache_btn_frame, text="☐ Deselect All", width=100, fg_color="#2b2b2b", hover_color="#3b3b3b", command=lambda: toggle_all_caches(False))
cache_deselect_all_btn.pack(side='right', padx=8, pady=8)

cache_select_all_btn = ctk.CTkButton(cache_btn_frame, text="☑ Select All", width=100, fg_color="#2b2b2b", hover_color="#3b3b3b", command=lambda: toggle_all_caches(True))
cache_select_all_btn.pack(side='right', padx=8, pady=8)

cache_scroll = ctk.CTkScrollableFrame(cachef)
cache_scroll.pack(fill='both', expand=True, pady=(0, 8))

cache_checkbox_vars = {}
cache_meta_data = {}

cache_bottom_frame = ctk.CTkFrame(cachef)
cache_bottom_frame.pack(fill='x')

cache_summary_lbl = ctk.CTkLabel(cache_bottom_frame, text="Select caches to clean.", font=ctk.CTkFont(size=14))
cache_summary_lbl.pack(side='left', padx=12, pady=12)

cache_clean_btn = ctk.CTkButton(cache_bottom_frame, text="🗑️ Clean Selected Caches (Safe)", fg_color="#ff5555", hover_color="#ff3333", height=40, font=ctk.CTkFont(size=14, weight="bold"), command=lambda: threading.Thread(target=cache_clean_selected, daemon=True).start())
cache_clean_btn.pack(side='right', padx=12, pady=12)

# ----------------------------
# Drive frame
# ----------------------------
drivef = frames['drive']
drivef.pack(padx=12, pady=12)
drive_lbl = ctk.CTkLabel(drivef, text="Drive Info", font=ctk.CTkFont(size=16, weight="bold"))
drive_lbl.pack(anchor='w', pady=(4,8))

drive_output = ScrolledText(drivef, bg="#1e1e1e", fg="#e0e0e0", insertbackground="white", font=OUTPUT_FONT)
drive_output.pack(fill='both', expand=True)

def show_drive_info():
    drive_output.delete('1.0','end')
    parts = psutil.disk_partitions(all=False)
    for p in parts:
        try:
            u = psutil.disk_usage(p.mountpoint)
        except Exception:
            continue
        drive_output.insert('end', f"💽 Device: {p.device}\n📍 Mount Point: {p.mountpoint}\n")
        drive_output.insert('end', f"   🧱 Total: {humanize.naturalsize(u.total)}\n   🟡 Used: {humanize.naturalsize(u.used)} ({u.percent}%)\n   🟢 Free: {humanize.naturalsize(u.free)}\n")
        drive_output.insert('end', "-"*80 + "\n")

drive_refresh_btn = ctk.CTkButton(drivef, text="Refresh Drive Info", command=show_drive_info)
drive_refresh_btn.pack(pady=6)
# show default drive info
show_drive_info()

# default view
show_frame('dashboard')

# ----------------------------
# Scan cancel helper
# ----------------------------
scan_events = {
    'dashboard': None,
    'visualizer': None
}

def cancel_scan(scope):
    ev = scan_events.get(scope)
    if ev:
        ev.set()
        status_var.set("Cancelling...")

# ----------------------------
# Chart showing helpers
# ----------------------------
dash_current_ext_summary = []
viz_current_ext_summary = []

def show_dash_chart():
    if not dash_current_ext_summary:
        return
    types = {ext if ext != "<no-ext>" else "(no ext)": size for ext, cnt, size in dash_current_ext_summary[:10]}
    if not types:
        return
    try:
        fig = Figure(figsize=(8,5))
        ax = fig.add_subplot(111)
        keys = list(types.keys())[::-1]
        vals = [s / (1024*1024*1024) for s in list(types.values())[::-1]]
        ax.barh(keys, vals, color="#1b6bff")
        ax.set_xlabel("Size (GB)", fontsize=12)
        ax.set_title("Top File Types by Size", fontsize=14, weight="bold")
        ax.grid(axis='x', linestyle='--', alpha=0.7)
        fig.tight_layout()
        show_figure_nonblocking(fig)
    except Exception:
        pass

def show_viz_chart():
    if not viz_current_ext_summary:
        return
    top_types = viz_current_ext_summary[:10]
    if not top_types:
        return
    try:
        fig = Figure(figsize=(8,8))
        ax = fig.add_subplot(111)
        labels = [e if e != "<no-ext>" else "(no ext)" for e, c, s in top_types]
        sizes = [s for e, c, s in top_types]
        explode = [0.05] * len(sizes)
        ax.pie(sizes, labels=labels, explode=explode, autopct='%1.1f%%', startangle=140, 
               textprops={'fontsize': 10})
        ax.set_title("Top File Types by Size", fontsize=14, weight="bold")
        fig.tight_layout()
        show_figure_nonblocking(fig)
    except Exception:
        pass

# ----------------------------
# Dashboard scan implementation
# ----------------------------
def dashboard_scan(path):
    global dash_cancel_event
    with dash_lock:
        if not path or not os.path.exists(path):
            root.after(0, lambda: messagebox.showwarning("Folder missing", "Please select a valid folder."))
            return

        # warn if root
        if os.path.abspath(path) in (os.path.abspath(os.sep), os.path.abspath(os.path.expanduser("~"))):
            ok = messagebox.askyesno("Confirm scan", f"You're about to scan '{path}'. This may take a long time. Continue?")
            if not ok:
                return

        # create cancel event for this scan
        dash_cancel_event = threading.Event()
        scan_events['dashboard'] = dash_cancel_event

        status_var.set("Scanning (dashboard)...")
        progressbar.set(0)
        for w in dash_output_scroll.winfo_children():
            w.destroy()
        dash_chart_btn.configure(state="disabled")

        # optional quick count for determinate progress (fast-ish)
        total_estimate = None
        try:
            total_estimate = quick_count_files(path)
        except Exception:
            total_estimate = None

    # progress callback will be called from worker thread; use root.after to safely update UI
    def progress_cb(processed, total_est, elapsed):
        def _ui():
            if total_est and total_est > 0:
                progress = min(processed / max(1, total_est), 1.0)
                progressbar.set(progress)
                status_var.set(f"Scanning... {processed:,}/{total_est:,} files — {humanize.naturalsize(0)} scanned")
            else:
                # indeterminate-style: show processed count and elapsed
                status_var.set(f"Scanning... {processed:,} files — {int(elapsed)}s elapsed")
            # no direct heavy GUI work here
        root.after(0, _ui)

    def worker():
        try:
            total_files, total_size, ext_summary, top_files = collect_folder_stats_streaming(
                path, progress_callback=progress_cb, total_estimate=total_estimate, cancel_event=dash_cancel_event
            )
            # render results in UI thread
            def _render():
                global dash_current_ext_summary
                dash_current_ext_summary = ext_summary
                
                for w in dash_output_scroll.winfo_children():
                    w.destroy()
                    
                summary_card = AccordionCard(dash_output_scroll, "Summary", default_open=True)
                summary_card.pack(fill='x', pady=4)
                sum_lbl = ctk.CTkLabel(summary_card.content_frame, text=f"📁 Path: {path}\n🧾 Files: {total_files:,}\n💾 Size: {humanize.naturalsize(total_size)}", font=ctk.CTkFont(size=14), justify="left", anchor="w")
                sum_lbl.pack(padx=12, pady=12, fill='x')
                
                top_card = AccordionCard(dash_output_scroll, "Top 20 Largest Files", default_open=False)
                top_card.pack(fill='both', expand=True, pady=4)
                top_box = ctk.CTkTextbox(top_card.content_frame, font=OUTPUT_FONT, height=250)
                top_box.pack(fill='both', expand=True, padx=4, pady=4)
                for i, (sz, fp) in enumerate(top_files[:20], 1):
                    name = Path(fp).name
                    if len(name) > 35: name = name[:32] + "..."
                    top_box.insert('end', f" {i:2d}. {humanize.naturalsize(sz):>10}  |  {name}\n      {fp}\n\n")
                top_box.configure(state="disabled")

                types_card = AccordionCard(dash_output_scroll, "File Types Breakdown", default_open=False)
                types_card.pack(fill='both', expand=True, pady=4)
                types_box = ctk.CTkTextbox(types_card.content_frame, font=OUTPUT_FONT, height=200)
                types_box.pack(fill='both', expand=True, padx=4, pady=4)
                for ext, cnt, size in ext_summary[:15]:
                    types_box.insert('end', f" {ext:>10}   • {cnt:7,} files   • {humanize.naturalsize(size):>10}\n")
                types_box.configure(state="disabled")

                status_var.set("Dashboard scan complete")
                progressbar.set(0)
                dash_chart_btn.configure(state="normal")
            root.after(0, _render)
        except Exception as e:
            root.after(0, lambda: messagebox.showerror("Scan error", str(e)))
            root.after(0, lambda: status_var.set("Scan error"))
        finally:
            # clear cancel event
            scan_events['dashboard'] = None

    threading.Thread(target=worker, daemon=True).start()

# ----------------------------
# Visualizer implementation
# ----------------------------
def folder_visualize(path):
    global viz_cancel_event
    with viz_lock:
        if not path or not os.path.exists(path):
            root.after(0, lambda: messagebox.showwarning("Invalid folder", "Please choose a valid folder path."))
            return

        # warn if root
        if os.path.abspath(path) in (os.path.abspath(os.sep), os.path.abspath(os.path.expanduser("~"))):
            ok = messagebox.askyesno("Confirm analysis", f"You're about to analyze '{path}'. This may take a long time. Continue?")
            if not ok:
                return

        viz_cancel_event = threading.Event()
        scan_events['visualizer'] = viz_cancel_event

        status_var.set("Analyzing folder (visualizer)...")
        progressbar.set(0)
        for w in viz_output_scroll.winfo_children():
            w.destroy()
        viz_chart_btn.configure(state="disabled")

        # optional estimate
        total_estimate = None
        try:
            total_estimate = quick_count_files(path)
        except Exception:
            total_estimate = None

    def progress_cb(processed, total_est, elapsed):
        def _ui():
            if total_est and total_est > 0:
                progressbar.set(min(processed / max(1, total_est), 1.0))
                status_var.set(f"Analyzing... {processed:,}/{total_est:,} files — {int(elapsed)}s")
            else:
                status_var.set(f"Analyzing... {processed:,} files — {int(elapsed)}s")
        root.after(0, _ui)

    def worker():
        try:
            total_files, total_size, ext_summary, top_files = collect_folder_stats_streaming(
                path, progress_callback=progress_cb, total_estimate=total_estimate, top_n=25, cancel_event=viz_cancel_event
            )
            def _render():
                global viz_current_ext_summary
                viz_current_ext_summary = ext_summary
                
                for w in viz_output_scroll.winfo_children():
                    w.destroy()
                    
                summary_card = AccordionCard(viz_output_scroll, "Analysis Summary", default_open=True)
                summary_card.pack(fill='x', pady=4)
                sum_lbl = ctk.CTkLabel(summary_card.content_frame, text=f"📂 Path: {path}\n🧾 Files: {total_files:,}\n💾 Size: {humanize.naturalsize(total_size)}", font=ctk.CTkFont(size=14), justify="left", anchor="w")
                sum_lbl.pack(padx=12, pady=12, fill='x')
                
                top_card = AccordionCard(viz_output_scroll, "Top 25 Largest Files", default_open=False)
                top_card.pack(fill='both', expand=True, pady=4)
                top_box = ctk.CTkTextbox(top_card.content_frame, font=OUTPUT_FONT, height=300)
                top_box.pack(fill='both', expand=True, padx=4, pady=4)
                for i, (sz, fp) in enumerate(top_files[:25], 1):
                    name = Path(fp).name
                    if len(name) > 35: name = name[:32] + "..."
                    top_box.insert('end', f" {i:2d}. {humanize.naturalsize(sz):>10}  |  {name}\n      {fp}\n\n")
                top_box.configure(state="disabled")

                types_card = AccordionCard(viz_output_scroll, "All File Types", default_open=False)
                types_card.pack(fill='both', expand=True, pady=4)
                types_box = ctk.CTkTextbox(types_card.content_frame, font=OUTPUT_FONT, height=300)
                types_box.pack(fill='both', expand=True, padx=4, pady=4)
                for ext, cnt, size in ext_summary:
                    types_box.insert('end', f" {ext:>10} | {cnt:7,} f | {humanize.naturalsize(size):>10}\n")
                types_box.configure(state="disabled")

                status_var.set("Folder visualization complete")
                progressbar.set(0)
                viz_chart_btn.configure(state="normal")
            root.after(0, _render)
        except Exception as e:
            root.after(0, lambda: messagebox.showerror("Analysis error", str(e)))
            root.after(0, lambda: status_var.set("Analysis error"))
        finally:
            scan_events['visualizer'] = None

    threading.Thread(target=worker, daemon=True).start()

# ----------------------------
# Cache scanning & cleaning
# ----------------------------
def _render_cache_items(summary, append=False):
    if not append:
        for widget in cache_scroll.winfo_children():
            widget.destroy()
        cache_checkbox_vars.clear()
        cache_meta_data.clear()

    for p, meta in summary.items():
        if p in cache_meta_data:
            continue
        
        cache_meta_data[p] = meta
        var = ctk.IntVar(value=0)
        cache_checkbox_vars[p] = var
        
        frame = ctk.CTkFrame(cache_scroll, fg_color="#1e1e1e", corner_radius=6)
        frame.pack(fill='x', padx=8, pady=4)
        
        cb = ctk.CTkCheckBox(frame, text="", variable=var, width=24)
        cb.pack(side='left', padx=(12, 4), pady=12)
        
        info_frame = ctk.CTkFrame(frame, fg_color="transparent")
        info_frame.pack(side='left', fill='x', expand=True, padx=8, pady=8)
        
        path_lbl = ctk.CTkLabel(info_frame, text=p, font=ctk.CTkFont(size=14, weight="bold"), anchor="w")
        path_lbl.pack(fill='x')
        
        stats_text = f"Size: {humanize.naturalsize(meta['size'])}  |  Files: {meta['files']:,}  |  Folders: {meta['folders']:,}"
        stats_lbl = ctk.CTkLabel(info_frame, text=stats_text, text_color="#aaaaaa", anchor="w")
        stats_lbl.pack(fill='x')

    total_found = sum(m['size'] for m in cache_meta_data.values())
    cache_summary_lbl.configure(text=f"Found {len(cache_meta_data)} locations. Total size: {humanize.naturalsize(total_found)}")
    status_var.set("Cache scan complete")
    progressbar.set(0)

def cache_scan_and_report():
    status_var.set("Scanning caches...")
    progressbar.set(0)
    summary = get_cache_summary(top_n=1)
    if not summary:
        for widget in cache_scroll.winfo_children():
            widget.destroy()
        lbl = ctk.CTkLabel(cache_scroll, text="No cache locations found.")
        lbl.pack(pady=20)
        status_var.set("Cache scan complete")
        return
    _render_cache_items(summary)

def cache_scan_custom(path):
    status_var.set(f"Scanning {path}...")
    progressbar.set(0)
    total_size = 0
    files_count = 0
    folders = set()
    for dirpath, dirnames, filenames in os.walk(path, topdown=True):
        dirnames[:] = [d for d in dirnames if not os.path.islink(os.path.join(dirpath, d)) and not os.path.ismount(os.path.join(dirpath, d))]
        folders.add(dirpath)
        for f in filenames:
            fp = os.path.join(dirpath, f)
            try:
                sz = os.path.getsize(fp)
                total_size += sz
                files_count += 1
            except Exception:
                continue
    summary = {path: {'size': total_size, 'files': files_count, 'folders': len(folders), 'top': []}}
    _render_cache_items(summary, append=True)
    status_var.set("Custom folder scanned")

def cache_clean_selected():
    selected_paths = [p for p, var in cache_checkbox_vars.items() if var.get() == 1]
    if not selected_paths:
        messagebox.showinfo("Nothing selected", "Please select at least one cache location to clean.")
        return
        
    confirm = messagebox.askyesno("Confirm clean", "This will move all contents within the selected cache folders to the Recycle Bin. Proceed?")
    if not confirm:
        return
        
    status_var.set("Moving items to Recycle Bin...")
    progressbar.set(0.5)
    
    cleaned_items = 0
    skipped_items = 0
    total_freed = 0
    
    for p in selected_paths:
        try:
            for item in os.listdir(p):
                item_path = os.path.join(p, item)
                try:
                    sz = 0
                    if os.path.isfile(item_path):
                        sz = os.path.getsize(item_path)
                    elif os.path.isdir(item_path):
                        sz = sum(os.path.getsize(os.path.join(dp, f)) for dp, _, fn in os.walk(item_path) for f in fn)
                    send2trash(item_path)
                    cleaned_items += 1
                    total_freed += sz
                except Exception:
                    skipped_items += 1
        except Exception:
            skipped_items += 1
            
    for widget in cache_scroll.winfo_children():
        widget.destroy()
    cache_checkbox_vars.clear()
    cache_meta_data.clear()
    cache_summary_lbl.configure(text="Clean complete.")
    
    messagebox.showinfo("Clean complete", f"Items moved to Recycle Bin: {cleaned_items}\nSkipped (in-use): {skipped_items}\nSpace Freed: {humanize.naturalsize(total_freed)}")
    status_var.set(f"Cleaned {humanize.naturalsize(total_freed)}")
    progressbar.set(0)

# ----------------------------
# Final housekeeping & mainloop
# ----------------------------
# show default drive info on startup already called above
show_frame('dashboard')

root.mainloop()
