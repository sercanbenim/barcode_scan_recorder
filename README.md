# Barcode Scan Recorder

Python desktop application for recording packing sessions, scanning barcodes from a live webcam feed,
and keeping searchable history of barcode detections.

## Features

- Live webcam preview with barcode detection using [pyzbar](https://github.com/NaturalHistoryMuseum/pyzbar)
- Optional MP4 recording of each session with OpenCV
- Automatic storage of detected barcode, timestamp, and related video path
- Search screen to filter detections by barcode fragment or date
- Daily report tab summarizing how many barcodes were captured per day

## Requirements

- Python 3.10+
- A webcam accessible from the computer running the program
- The following Python packages (install via `pip install -r requirements.txt`):
  - `opencv-python`
  - `pyzbar`
  - `Pillow`

> **Note:** On Linux you may need to install additional system packages for `pyzbar`
> (e.g., `sudo apt-get install libzbar0`).

## Running the App

```bash
python app.py
```

The first launch creates a SQLite database at `data/records.db`. Recordings are saved as MP4 files under `recordings/<YYYYMMDD>/`.

### Capture Tab

1. Press **Start Recording** to save the current session to disk. A dated MP4 file is created automatically.
2. Place the tracking barcode in front of the camera. Each unique scan is stored with the timestamp and the active recording path.
3. Press **Stop Recording** when finished. Use **Refresh List** to reload the capture history on the right side of the window.

### Search Tab

Enter a partial barcode or date (`YYYY-MM-DD`) and click **Search**. Results show the timestamp and the associated video file.

### Reports Tab

Displays a daily summary of barcode detections. Click **Refresh Report** to update the list.

## Project Structure

```
app.py              # Tkinter application entry point
requirements.txt    # Python dependencies
recordings/         # Created at runtime to store MP4 files
data/records.db     # SQLite database with capture metadata
```

## Development Tips

- Adjust `FRAME_UPDATE_MS` in `app.py` to tune the UI refresh rate if CPU usage is high.
- Change the video capture index in `_start_video_capture` if you need to select a different camera device.
