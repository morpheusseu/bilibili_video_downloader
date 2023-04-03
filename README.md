# bilibili_video_downloader

# Introduction

This repository contains a video/audio downloader for Bilibili that uses your unique credentials. It has several modes:

1. Use bv id of the video to download
2. Use favorite list id to download all videos from the list
3. Use upper's uid and its owned favorite list's name to match desired favorite list and download all videos from it

All downloads can choose type from (".mp4", ".wav", ".mp3"). A PyQt5 GUI is established for usability for users, but it has a shortcoming that you can not interact when task is running and output is on the console instead of on the PyQt5 GUI.

The work is based on the achievement of [bilibili_api](https://github.com/MoyuScript/bilibili-api).

## Usage

To use this downloader, you can follow these steps:

1. Clone this repository to your local machine.
2. Install Python 3.x if you haven't already.
3. Install the required packages by running `pip install -r requirements.txt` in your terminal.
4. Run `python main_window.py` in your terminal to start the GUI.

## Modes

This downloader has three modes that allow you to download videos and audio from Bilibili in different ways:

### Mode 1

In mode 1, you can download a single video by entering its bv id.

### Mode 2

In mode 2, you can download all videos from a favorite list by entering its id.

### Mode 3

In mode 3, you can download all videos from a favorite list owned by a specific user by entering their uid and the name of the list.

## License

This project is licensed under the MIT License - see the [LICENSE](LICENSE) file for details.
