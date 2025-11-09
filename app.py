"""Barcode scan recorder application using Tkinter and OpenCV.

This GUI shows a live webcam feed, scans barcodes via pyzbar and
persists detections in a SQLite database. Operators can optionally
record a video clip for each packing session. Video clips and barcode
metadata can be searched and aggregated by day within the app.
"""

from __future__ import annotations

import csv
import datetime as dt
import os
import sqlite3
import subprocess
import sys
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional

import cv2
from PIL import Image, ImageTk
from pyzbar import pyzbar
import tkinter as tk
from tkinter import ttk, messagebox

# Paths
APP_ROOT = Path(__file__).parent
DATA_DIR = APP_ROOT / "data"
RECORDINGS_DIR = APP_ROOT / "recordings"
DB_PATH = DATA_DIR / "records.db"

FRAME_UPDATE_MS = 30  # ~33fps
MIN_DETECTION_INTERVAL_SEC = 3  # prevent duplicate inserts


@dataclass
class BarcodeDetection:
    value: str
    timestamp: dt.datetime
    video_path: Optional[str]


class BarcodeRecorderApp(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("Barcode Scan Recorder")
        self.protocol("WM_DELETE_WINDOW", self.on_close)

        self.capture = None
        self.current_frame = None
        self.video_writer = None
        self.recording_path = None
        self.is_recording = False
        self.last_detection_time = dt.datetime.min
        self.last_detection_value = ""

        self._init_database()
        self._build_ui()
        self._start_video_capture()

    # ------------------------------------------------------------------
    # Database utilities
    def _init_database(self) -> None:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(DB_PATH)
        with conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS captures (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    barcode TEXT NOT NULL,
                    detected_at TEXT NOT NULL,
                    video_path TEXT
                )
                """
            )
        conn.close()

    def insert_detection(self, detection: BarcodeDetection) -> None:
        conn = sqlite3.connect(DB_PATH)
        with conn:
            conn.execute(
                "INSERT INTO captures (barcode, detected_at, video_path) VALUES (?, ?, ?)",
                (detection.value, detection.timestamp.isoformat(), detection.video_path),
            )
        conn.close()

    def query_detections(
        self, barcode: Optional[str] = None, date: Optional[dt.date] = None
    ) -> Iterable[tuple]:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        query = "SELECT id, barcode, detected_at, IFNULL(video_path, '') AS video_path FROM captures"
        conditions = []
        params: list = []
        if barcode:
            conditions.append("barcode LIKE ?")
            params.append(f"%{barcode}%")
        if date:
            conditions.append("date(detected_at) = ?")
            params.append(date.isoformat())
        if conditions:
            query += " WHERE " + " AND ".join(conditions)
        query += " ORDER BY detected_at DESC"
        rows = conn.execute(query, params).fetchall()
        conn.close()
        return rows

    def query_daily_counts(self) -> Iterable[tuple]:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT date(detected_at) AS day, COUNT(*) AS total FROM captures GROUP BY day ORDER BY day DESC"
        ).fetchall()
        conn.close()
        return rows

    # ------------------------------------------------------------------
    # UI construction
    def _build_ui(self) -> None:
        notebook = ttk.Notebook(self)
        notebook.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)

        self.capture_tab = ttk.Frame(notebook)
        self.search_tab = ttk.Frame(notebook)
        self.report_tab = ttk.Frame(notebook)

        notebook.add(self.capture_tab, text="Capture")
        notebook.add(self.search_tab, text="Search")
        notebook.add(self.report_tab, text="Reports")

        self._build_capture_tab()
        self._build_search_tab()
        self._build_report_tab()

    def _build_capture_tab(self) -> None:
        video_frame = ttk.Frame(self.capture_tab)
        video_frame.pack(side=tk.LEFT, padx=10, pady=10)

        self.video_label = ttk.Label(video_frame)
        self.video_label.pack()

        controls_frame = ttk.Frame(self.capture_tab)
        controls_frame.pack(side=tk.RIGHT, fill=tk.Y, padx=10, pady=10)

        self.status_var = tk.StringVar(value="No barcode detected yet.")
        status_label = ttk.Label(controls_frame, textvariable=self.status_var, wraplength=220)
        status_label.pack(pady=(0, 10))

        self.record_button = ttk.Button(controls_frame, text="Start Recording", command=self.toggle_recording)
        self.record_button.pack(fill=tk.X)

        self.recording_indicator = ttk.Label(controls_frame, text="Recording: OFF", foreground="red")
        self.recording_indicator.pack(pady=5)

        self.capture_list = ttk.Treeview(
            controls_frame,
            columns=("barcode", "detected_at", "video"),
            show="headings",
            height=10,
        )
        self.capture_list.heading("barcode", text="Barcode")
        self.capture_list.heading("detected_at", text="Detected At")
        self.capture_list.heading("video", text="Video Path")
        self.capture_list.column("barcode", width=140)
        self.capture_list.column("detected_at", width=160)
        self.capture_list.column("video", width=180)
        self.capture_list.pack(fill=tk.BOTH, expand=True, pady=(10, 0))

        button_row = ttk.Frame(controls_frame)
        button_row.pack(fill=tk.X, pady=5)

        refresh_btn = ttk.Button(button_row, text="Refresh", command=self.refresh_capture_list)
        refresh_btn.pack(side=tk.LEFT, expand=True, fill=tk.X)

        open_btn = ttk.Button(button_row, text="Open Video", command=self.open_selected_capture_video)
        open_btn.pack(side=tk.LEFT, expand=True, fill=tk.X, padx=(5, 0))

        self.refresh_capture_list()

    def _build_search_tab(self) -> None:
        form = ttk.Frame(self.search_tab)
        form.pack(fill=tk.X, padx=10, pady=10)

        ttk.Label(form, text="Barcode contains:").grid(row=0, column=0, sticky=tk.W, pady=5)
        self.search_barcode_var = tk.StringVar()
        ttk.Entry(form, textvariable=self.search_barcode_var).grid(row=0, column=1, sticky=tk.EW, padx=5)

        ttk.Label(form, text="Date (YYYY-MM-DD):").grid(row=1, column=0, sticky=tk.W, pady=5)
        self.search_date_var = tk.StringVar()
        ttk.Entry(form, textvariable=self.search_date_var).grid(row=1, column=1, sticky=tk.EW, padx=5)

        form.columnconfigure(1, weight=1)

        actions = ttk.Frame(self.search_tab)
        actions.pack(fill=tk.X, padx=10)

        ttk.Button(actions, text="Search", command=self.perform_search).pack(side=tk.LEFT)
        ttk.Button(actions, text="Clear", command=self.clear_search).pack(side=tk.LEFT, padx=5)
        ttk.Button(actions, text="Open Video", command=self.open_selected_search_video).pack(side=tk.LEFT, padx=(5, 0))

        self.search_results = ttk.Treeview(
            self.search_tab,
            columns=("barcode", "detected_at", "video"),
            show="headings",
            height=15,
        )
        self.search_results.heading("barcode", text="Barcode")
        self.search_results.heading("detected_at", text="Detected At")
        self.search_results.heading("video", text="Video Path")
        self.search_results.column("barcode", width=160)
        self.search_results.column("detected_at", width=180)
        self.search_results.column("video", width=200)
        self.search_results.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)

    def _build_report_tab(self) -> None:
        ttk.Label(self.report_tab, text="Daily Recorded Barcodes", font=("TkDefaultFont", 12, "bold")).pack(
            anchor=tk.W, padx=10, pady=(10, 0)
        )

        self.report_tree = ttk.Treeview(
            self.report_tab,
            columns=("day", "total"),
            show="headings",
            height=15,
        )
        self.report_tree.heading("day", text="Date")
        self.report_tree.heading("total", text="Total Detections")
        self.report_tree.column("day", width=140)
        self.report_tree.column("total", width=150)
        self.report_tree.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)

        action_row = ttk.Frame(self.report_tab)
        action_row.pack(fill=tk.X, padx=10, pady=(0, 10))

        ttk.Button(action_row, text="Refresh Report", command=self.refresh_report).pack(side=tk.RIGHT)
        ttk.Button(action_row, text="Export CSV", command=self.export_daily_report).pack(side=tk.RIGHT, padx=(0, 5))

        self.refresh_report()

    # ------------------------------------------------------------------
    # Capture logic
    def _start_video_capture(self) -> None:
        self.capture = cv2.VideoCapture(0)
        if not self.capture.isOpened():
            messagebox.showerror("Camera Error", "Unable to access the camera.")
            return
        self._update_frame()

    def _update_frame(self) -> None:
        if not self.capture or not self.capture.isOpened():
            return

        ret, frame = self.capture.read()
        if not ret:
            self.after(FRAME_UPDATE_MS, self._update_frame)
            return

        self.current_frame = frame
        self._process_frame_for_barcodes(frame)

        frame_rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        img = Image.fromarray(frame_rgb)
        imgtk = ImageTk.PhotoImage(image=img)

        self.video_label.imgtk = imgtk
        self.video_label.configure(image=imgtk)

        if self.is_recording and self.video_writer is not None:
            self.video_writer.write(frame)

        self.after(FRAME_UPDATE_MS, self._update_frame)

    def _process_frame_for_barcodes(self, frame) -> None:
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        barcodes = pyzbar.decode(gray)

        for barcode in barcodes:
            barcode_data = barcode.data.decode("utf-8")
            now = dt.datetime.now()
            if (
                barcode_data != self.last_detection_value
                or (now - self.last_detection_time).total_seconds() > MIN_DETECTION_INTERVAL_SEC
            ):
                self.last_detection_value = barcode_data
                self.last_detection_time = now
                detection = BarcodeDetection(
                    value=barcode_data,
                    timestamp=now,
                    video_path=self.recording_path if self.is_recording else None,
                )
                threading.Thread(target=self.insert_detection, args=(detection,), daemon=True).start()
                self.status_var.set(f"Detected barcode: {barcode_data} at {now:%Y-%m-%d %H:%M:%S}")
                self.refresh_capture_list_async()
                self.refresh_report_async()

            x, y, w, h = barcode.rect
            cv2.rectangle(frame, (x, y), (x + w, y + h), (0, 255, 0), 2)
            cv2.putText(
                frame,
                barcode_data,
                (x, max(0, y - 10)),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.6,
                (0, 255, 0),
                2,
            )

    def toggle_recording(self) -> None:
        if self.is_recording:
            self._stop_recording()
        else:
            self._start_recording()

    def _start_recording(self) -> None:
        if not self.capture or not self.capture.isOpened():
            messagebox.showwarning("Camera", "Camera not available.")
            return

        RECORDINGS_DIR.mkdir(parents=True, exist_ok=True)
        timestamp = dt.datetime.now()
        date_dir = RECORDINGS_DIR / timestamp.strftime("%Y%m%d")
        date_dir.mkdir(exist_ok=True)
        filename = timestamp.strftime("%H%M%S") + ".mp4"
        self.recording_path = str(date_dir / filename)

        fourcc = cv2.VideoWriter_fourcc(*"mp4v")
        width = int(self.capture.get(cv2.CAP_PROP_FRAME_WIDTH))
        height = int(self.capture.get(cv2.CAP_PROP_FRAME_HEIGHT))
        fps = self.capture.get(cv2.CAP_PROP_FPS) or 30.0
        self.video_writer = cv2.VideoWriter(self.recording_path, fourcc, fps, (width, height))

        if not self.video_writer.isOpened():
            messagebox.showerror("Recording", "Unable to start video recording.")
            self.video_writer = None
            self.recording_path = None
            return

        self.is_recording = True
        self.record_button.configure(text="Stop Recording")
        self.recording_indicator.configure(text="Recording: ON", foreground="green")
        self.status_var.set(f"Recording started: {self.recording_path}")

    def _stop_recording(self) -> None:
        self.is_recording = False
        if self.video_writer is not None:
            self.video_writer.release()
            self.video_writer = None
        self.record_button.configure(text="Start Recording")
        self.recording_indicator.configure(text="Recording: OFF", foreground="red")
        self.status_var.set("Recording stopped.")
        self.recording_path = None

    # ------------------------------------------------------------------
    # Refresh helpers
    def refresh_capture_list(self) -> None:
        for row in self.capture_list.get_children():
            self.capture_list.delete(row)
        rows = self.query_detections()
        for row in rows:
            self.capture_list.insert("", tk.END, values=(row["barcode"], row["detected_at"], row["video_path"]))

    def refresh_capture_list_async(self) -> None:
        self.after(0, self.refresh_capture_list)

    def _get_first_selected_value(self, tree: ttk.Treeview) -> Optional[str]:
        selection = tree.selection()
        if not selection:
            messagebox.showinfo("Open Video", "Select a row that has an associated video.")
            return None
        video_path = tree.set(selection[0], "video")
        if not video_path:
            messagebox.showinfo("Open Video", "The selected row has no stored video path.")
            return None
        return video_path

    def open_selected_capture_video(self) -> None:
        path = self._get_first_selected_value(self.capture_list)
        if path:
            self._open_video_file(path)

    def open_selected_search_video(self) -> None:
        path = self._get_first_selected_value(self.search_results)
        if path:
            self._open_video_file(path)

    def _open_video_file(self, path: str) -> None:
        file_path = Path(path)
        if not file_path.exists():
            messagebox.showerror("Open Video", f"File not found:\n{file_path}")
            return

        try:
            if sys.platform.startswith("darwin"):
                subprocess.Popen(["open", str(file_path)])
            elif os.name == "nt":
                os.startfile(str(file_path))  # type: ignore[attr-defined]
            else:
                subprocess.Popen(["xdg-open", str(file_path)])
        except Exception as exc:  # pragma: no cover - platform specific
            messagebox.showerror("Open Video", f"Could not open video file:\n{exc}")

    def perform_search(self) -> None:
        barcode = self.search_barcode_var.get().strip()
        date_str = self.search_date_var.get().strip()
        date_val = None
        if date_str:
            try:
                date_val = dt.datetime.strptime(date_str, "%Y-%m-%d").date()
            except ValueError:
                messagebox.showerror("Invalid date", "Use YYYY-MM-DD format.")
                return

        rows = self.query_detections(barcode=barcode or None, date=date_val)
        for row_id in self.search_results.get_children():
            self.search_results.delete(row_id)
        for row in rows:
            self.search_results.insert(
                "",
                tk.END,
                values=(row["barcode"], row["detected_at"], row["video_path"]),
            )

    def clear_search(self) -> None:
        self.search_barcode_var.set("")
        self.search_date_var.set("")
        for row in self.search_results.get_children():
            self.search_results.delete(row)

    def refresh_report(self) -> None:
        for row in self.report_tree.get_children():
            self.report_tree.delete(row)
        rows = self.query_daily_counts()
        for row in rows:
            self.report_tree.insert("", tk.END, values=(row["day"], row["total"]))

    def refresh_report_async(self) -> None:
        self.after(0, self.refresh_report)

    def export_daily_report(self) -> None:
        rows = list(self.query_daily_counts())
        if not rows:
            messagebox.showinfo("Export Report", "No data available to export.")
            return

        export_dir = DATA_DIR
        export_dir.mkdir(parents=True, exist_ok=True)
        filename = f"daily_report_{dt.datetime.now():%Y%m%d_%H%M%S}.csv"
        export_path = export_dir / filename

        with export_path.open("w", newline="", encoding="utf-8") as csvfile:
            writer = csv.writer(csvfile)
            writer.writerow(["Date", "Total Detections"])
            for row in rows:
                writer.writerow([row["day"], row["total"]])

        messagebox.showinfo("Export Report", f"Report saved to:\n{export_path}")

    # ------------------------------------------------------------------
    def on_close(self) -> None:
        self._stop_recording()
        if self.capture and self.capture.isOpened():
            self.capture.release()
        cv2.destroyAllWindows()
        self.destroy()


def main() -> None:
    app = BarcodeRecorderApp()
    app.mainloop()


if __name__ == "__main__":
    main()
