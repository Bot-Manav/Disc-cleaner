# Smart Space Extractor

A modern Windows desktop application for disk space analysis and cache cleaning.



## 🔍 What This Application Does

Smart Space Extractor scans selected folders and system locations to:

Measure total storage usage

Identify the largest files consuming disk space

Group files by type (extension) and calculate total size per type

Visualize disk usage using charts

Locate common cache and temporary folders

Safely clean cache files by sending them to the Recycle Bin

Display real-time drive usage information

All scanning operations are performed locally on the user’s system.
No data is uploaded, transmitted, or shared.


# ✨ Key Features
## 📁 Folder & Disk Analysis

Recursive folder scanning with mount-point and symlink protection

Accurate file size aggregation

Detection of the top largest files

File-type analysis by extension

Human-readable size formatting (GB, MB, etc.)

## 📊 Visual Insights

Bar charts and pie charts generated using Matplotlib

Visual breakdown of space usage by file type

Charts open in a separate viewer to avoid UI blocking

## 🧹 Cache Scanner & Cleaner

Automatic detection of common Windows cache locations

Displays cache size, file count, and folder count

Safe cleaning using Recycle Bin (no permanent deletion)

Skips files that are in use or protected

## 💽 Drive Information

Displays mounted drives and partitions

Shows total, used, and free space

Usage percentage per drive

## ⚙️ Performance & Safety

Non-blocking background scans (multi-threaded)

Cancelable scans at any time

Progress tracking and status updates

Handles permission errors gracefully

## 🛠️ Technology Stack

Python

CustomTkinter – modern dark-mode UI

psutil – disk and system statistics

Matplotlib – data visualization

send2trash – safe file deletion

Inno Setup – Windows installer

## 🖥️ Platform Support

Windows (Primary & Tested)

Designed for local execution only

No administrator privileges required for normal operation


## 🔐 Privacy & Security

No internet access required

No telemetry or background services

No automatic file deletion

Cache cleaning uses Recycle Bin for safety

User must explicitly confirm destructive actions


## Installation
### Windows Installer
Download the `.exe` from the **RELEASES** and run it.


