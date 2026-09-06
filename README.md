# compress-mg

`ffmpeg` wrapper to compress videos to a desired size.

It is highly recommended to use [LosslessCut](https://github.com/mifi/lossless-cut) to trim your videos before feeding them into this program.

Main goal was to answer the "What's the best bitrate I can use while also making it sendable on Discord?" question.

## Requirements

You'll need `ffmpeg` (which includes `ffprobe`) on your PATH.

You can use `winget` to install it:
`winget install Gyan.FFmpeg`
It should add it to your PATH automatically.

## Installation

Either download a release or compile the program yourself.

You can build the program yourself by cloning the repo, installing the dependencies from `requirements.txt`, and running `pyinstaller --onedir compress-mg.py` in your console. The use of a venv is highly recommended.

## Usage

Just run the program. Keep the console window open, the GUI depends on it.

Should be pretty self-explanatory, but you can add videos to the queue by browsing for them or drag-dropping them onto the GUI.

Click "Start" to begin working on the queue. "Clear" will clear *all* queued items; the file currently being encoded keeps going and stays in the list. Use "Stop" first if you want to cancel it.

