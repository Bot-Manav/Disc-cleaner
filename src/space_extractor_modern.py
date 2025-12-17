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

# ----------------------------
# Dashboard frame
# ----------------------------
dash = frames['dashboard']
dash.pack(padx=12, pady=12)

lbl = ctk.CTkLabel(dash, text="Dashboard - Quick Folder Scan", font=ctk.CTkFont(size=16, weight="bold"))
lbl.pack(anchor='w', pady=(4,8))

dash_select_frame = ctk.CTkFrame(dash)
dash_select_frame.pack(fill='x', pady=(4,8))

dash_path_var = ctk.StringVar()
dash_entry = ctk.CTkEntry(dash_select_frame, textvariable=dash_path_var, width=640)
dash_entry.grid(row=0, column=0, padx=(8,8), pady=8)

# cancel event for dashboard scan
dash_cancel_event = None
dash_lock = threading.Lock()

def choose_dash_folder():
    p = filedialog.askdirectory()
    if p:
        dash_path_var.set(p)
        threading.Thread(target=dashboard_scan, args=(p,), daemon=True).start()

dash_browse_btn = ctk.CTkButton(dash_select_frame, text="Browse", command=choose_dash_folder)
dash_browse_btn.grid(row=0, column=1, padx=6)

def dash_scan_button():
    p = dash_path_var.get()
    threading.Thread(target=dashboard_scan, args=(p,), daemon=True).start()

dash_scan_btn = ctk.CTkButton(dash_select_frame, text="Scan", command=dash_scan_button)
dash_scan_btn.grid(row=0, column=2, padx=6)

dash_cancel_btn = ctk.CTkButton(dash_select_frame, text="Cancel Scan", fg_color="#ff6666", hover_color="#ff3333",
                               command=lambda: cancel_scan('dashboard'))
dash_cancel_btn.grid(row=0, column=3, padx=6)

dash_output = ScrolledText(dash, height=20, bg="#101010", fg="#00ff99", font=OUTPUT_FONT)
dash_output.pack(fill='both', expand=True, pady=(8,0))

# ----------------------------
# Folder Visualizer frame
# ----------------------------
viz = frames['visualizer']
viz.pack(padx=12, pady=12)

v_lbl = ctk.CTkLabel(viz, text="Folder Visualizer", font=ctk.CTkFont(size=16, weight="bold"))
v_lbl.pack(anchor='w', pady=(4,8))

viz_select_frame = ctk.CTkFrame(viz)
viz_select_frame.pack(fill='x', pady=(4,8))

viz_path_var = ctk.StringVar()
viz_entry = ctk.CTkEntry(viz_select_frame, textvariable=viz_path_var, width=640)
viz_entry.grid(row=0, column=0, padx=(8,8), pady=8)

# cancel event for viz scan
viz_cancel_event = None
viz_lock = threading.Lock()

def choose_viz_folder():
    p = filedialog.askdirectory()
    if p:
        viz_path_var.set(p)

viz_browse_btn = ctk.CTkButton(viz_select_frame, text="Browse", command=choose_viz_folder)
viz_browse_btn.grid(row=0, column=1, padx=6)

def viz_scan_button():
    p = viz_path_var.get()
    threading.Thread(target=folder_visualize, args=(p,), daemon=True).start()

viz_scan_btn = ctk.CTkButton(viz_select_frame, text="Analyze Folder", command=viz_scan_button)
viz_scan_btn.grid(row=0, column=2, padx=6)

viz_cancel_btn = ctk.CTkButton(viz_select_frame, text="Cancel Analysis", fg_color="#ff6666", hover_color="#ff3333",
                              command=lambda: cancel_scan('visualizer'))
viz_cancel_btn.grid(row=0, column=3, padx=6)

# left: stats text, right: list of types
viz_pane = ctk.CTkFrame(viz)
viz_pane.pack(fill='both', expand=True, pady=(8,0))

viz_left = ctk.CTkFrame(viz_pane)
viz_left.pack(side='left', fill='both', expand=True, padx=(4,8))
viz_right = ctk.CTkFrame(viz_pane, width=320)
viz_right.pack(side='right', fill='y', padx=(8,4))

viz_output = ScrolledText(viz_left, bg="#0f0f0f", fg="#aaffc4", font=OUTPUT_FONT)
viz_output.pack(fill='both', expand=True)

types_listbox = ctk.CTkTextbox(viz_right, width=300, height=400)
types_listbox.pack(padx=8, pady=8)

# ----------------------------
# Cache frame
# ----------------------------
cachef = frames['cache']
cachef.pack(padx=12, pady=12)

c_lbl = ctk.CTkLabel(cachef, text="Cache Cleaner", font=ctk.CTkFont(size=16, weight="bold"))
c_lbl.pack(anchor='w', pady=(4,8))

cache_output = ScrolledText(cachef, height=18, bg="#0f0f0f", fg="#b9a6ff", font=OUTPUT_FONT)
cache_output.pack(fill='both', expand=True, pady=(8,0))

cache_btn_frame = ctk.CTkFrame(cachef)
cache_btn_frame.pack(fill='x', pady=(8,6))
cache_scan_btn = ctk.CTkButton(cache_btn_frame, text="Scan Cache", command=lambda: threading.Thread(target=cache_scan_and_report, daemon=True).start())
cache_scan_btn.pack(side='left', padx=8)
cache_clean_btn = ctk.CTkButton(cache_btn_frame, text="Clean Cache (safe)", fg_color="#ff5555", hover_color="#ff3333", command=lambda: threading.Thread(target=cache_clean_all, daemon=True).start())
cache_clean_btn.pack(side='left', padx=8)

# ----------------------------
# Drive frame
# ----------------------------
drivef = frames['drive']
drivef.pack(padx=12, pady=12)
drive_lbl = ctk.CTkLabel(drivef, text="Drive Info", font=ctk.CTkFont(size=16, weight="bold"))
drive_lbl.pack(anchor='w', pady=(4,8))

drive_output = ScrolledText(drivef, bg="#0f0f0f", fg="#8fe0ff", font=OUTPUT_FONT)
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
        dash_output.delete('1.0','end')

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
                dash_output.insert('end', f"📁 Scanned Folder:\n   {path}\n\n")
                dash_output.insert('end', f"🧾 Total Files: {total_files:,}\n💾 Total Size: {humanize.naturalsize(total_size)}\n")
                dash_output.insert('end', "\n─────────────────────────────\n")
                dash_output.insert('end', "🔥 Top Files:\n\n")
                for i, (sz, fp) in enumerate(top_files[:20], 1):
                    dash_output.insert('end', f" {i:2}. {humanize.naturalsize(sz):>8}  —  {Path(fp).name}\n     📍 {fp}\n\n")

                dash_output.insert('end', "─────────────────────────────\n")
                dash_output.insert('end', "📊 Top File Types by Total Size:\n\n")
                for ext, cnt, size in ext_summary[:15]:
                    dash_output.insert('end', f" {ext:>8}   • {cnt:6,} files   • {humanize.naturalsize(size)}\n")

                # small chart non-blocking
                types = {ext if ext != "<no-ext>" else "(no ext)": size for ext, cnt, size in ext_summary[:10]}
                if types:
                    try:
                        fig = Figure(figsize=(6,4))
                        ax = fig.add_subplot(111)
                        keys = list(types.keys())[::-1]
                        vals = [s / (1024*1024*1024) for s in list(types.values())[::-1]]
                        ax.barh(keys, vals)
                        ax.set_xlabel("Size (GB)")
                        ax.set_title("Top file types by size")
                        fig.tight_layout()
                        show_figure_nonblocking(fig)
                    except Exception:
                        pass

                status_var.set("Dashboard scan complete")
                progressbar.set(0)
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
        viz_output.delete('1.0','end')
        try:
            types_listbox.delete('0.0', 'end')
        except Exception:
            try:
                types_listbox.delete('1.0', 'end')
            except Exception:
                pass

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
                viz_output.insert('end', f"📂 Folder Analyzed:\n   {path}\n\n")
                viz_output.insert('end', f"🧾 Total Files: {total_files:,}\n💾 Total Size: {humanize.naturalsize(total_size)}\n")
                viz_output.insert('end', "\n─────────────────────────────\n")
                viz_output.insert('end', "🔥 Top 25 Largest Files:\n\n")
                for i, (sz, fp) in enumerate(top_files[:25], 1):
                    viz_output.insert('end', f" {i:2}. {humanize.naturalsize(sz):>8}  —  {Path(fp).name}\n     📍 {fp}\n\n")

                viz_output.insert('end', "─────────────────────────────\n")
                viz_output.insert('end', "📊 File Types (by size):\n\n")
                for ext, cnt, size in ext_summary:
                    line = f" {ext:>8}  {cnt:6} files — {humanize.naturalsize(size)}\n"
                    viz_output.insert('end', line)
                    try:
                        types_listbox.insert('0.0', line)
                    except Exception:
                        try:
                            types_listbox.insert("end", line)
                        except Exception:
                            pass

                # pie chart non-blocking
                top_types = ext_summary[:10]
                if top_types:
                    try:
                        fig = Figure(figsize=(6,6))
                        ax = fig.add_subplot(111)
                        labels = [e if e != "<no-ext>" else "(no ext)" for e, c, s in top_types]
                        sizes = [s for e, c, s in top_types]
                        explode = [0.04] * len(sizes)
                        ax.pie(sizes, labels=labels, explode=explode, autopct='%1.1f%%', startangle=140)
                        ax.set_title("Top file types by size")
                        fig.tight_layout()
                        show_figure_nonblocking(fig)
                    except Exception:
                        pass

                status_var.set("Folder visualization complete")
                progressbar.set(0)
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
def cache_scan_and_report():
    status_var.set("Scanning caches...")
    progressbar.set(0)
    cache_output.delete('1.0','end')
    summary = get_cache_summary(top_n=10)
    if not summary:
        cache_output.insert('end', "No cache locations found.\n")
        status_var.set("Cache scan complete")
        return

    total_all = 0
    for p, meta in summary.items():
        cache_output.insert('end', f"🗑️ Cache Location:\n   {p}\n")
        cache_output.insert('end', f"   📦 Size: {humanize.naturalsize(meta['size'])}\n   📄 Files: {meta['files']:,}   📁 Folders: {meta['folders']:,}\n\n")
        total_all += meta['size']
        if meta['top']:
            cache_output.insert('end', "   Top items:\n")
            for sz, fp in meta['top']:
                cache_output.insert('end', f"     {humanize.naturalsize(sz)} - {fp}\n")
        cache_output.insert('end', "-"*80 + "\n")
    cache_output.insert('end', f"\nTotal removable cache (sum): {humanize.naturalsize(total_all)}\n")
    status_var.set("Cache scan complete")
    progressbar.set(0)

def cache_clean_all():
    confirm = messagebox.askyesno("Confirm clean", "This will attempt to remove cache files found in common cache locations. Files in use will be skipped. Proceed?")
    if not confirm:
        return
    status_var.set("Cleaning cache (may take a while)...")
    progressbar.set(0)
    summary = get_cache_summary(top_n=10)
    cleaned = 0
    skipped = 0
    errors = 0
    for p, meta in summary.items():
        for dirpath, dirnames, filenames in os.walk(p):
            for f in filenames:
                fp = os.path.join(dirpath, f)
                try:
                    send2trash(fp)  # safe send to recycle bin
                    cleaned += 1
                except Exception:
                    # could be in-use/protected or permission denied
                    skipped += 1
            # try removing empty folders
            for d in dirnames:
                dp = os.path.join(dirpath, d)
                try:
                    if not os.listdir(dp):
                        os.rmdir(dp)
                except Exception:
                    pass
    cache_scan_and_report()
    messagebox.showinfo("Clean complete", f"Attempted to remove cache files.\nCleaned: {cleaned}\nSkipped (in-use/protected): {skipped}")
    status_var.set("Cache cleaned (best-effort)")

# ----------------------------
# Final housekeeping & mainloop
# ----------------------------
# show default drive info on startup already called above
show_frame('dashboard')

root.mainloop()
