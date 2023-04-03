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

In mode 1, you can download a single video by entering its bv id.

In mode 2, you can download all videos from a favorite list by entering its id.

In mode 3, you can download all videos from a favorite list owned by a specific user by entering their uid and the name of the list.

## License

## This project is licensed under the MIT License - see the [LICENSE](LICENSE) file for details.

---

# 介绍

这个仓库包含一个 Bilibili 的视频/音频下载器，可以使用您的唯一凭据。它有几种模式：

1. 使用视频的 bv id 下载
2. 使用收藏夹 id 下载列表中的所有视频
3. 使用上级 uid 及其拥有的收藏夹名称来匹配所需的收藏夹，并从中下载所有视频

所有下载都可以选择类型（“.mp4”，“.wav”，“.mp3”）。为了方便用户，建立了一个 PyQt5 GUI，但它有一个缺点，即在任务运行时无法交互，并且输出在控制台上而不是在 PyQt5 GUI 上。

这项工作基于[bilibili_api](https://github.com/MoyuScript/bilibili-api)的成果。

---

# 用法

要使用此下载器，您可以按照以下步骤操作：

1. 将此存储库克隆到本地计算机。
2. 如果尚未安装，请安装 Python 3.x。
3. 运行 pip install -r requirements.txt 以在终端中安装所需的软件包。
4. 在终端中运行 python main_window.py 以启动 GUI。

---

# 模式

此下载器有三种模式，可让您以不同的方式从 Bilibili 下载视频和音频：

## 模式 1

在模式 1 中，您可以通过输入其 bv id 来下载单个视频。

## 模式 2

在模式 2 中，您可以通过输入其 id 来从收藏夹中下载所有视频。

## 模式 3

在模式 3 中，您可以通过输入其 uid 和列表名称来下载特定用户拥有的收藏夹中的所有视频。

---

## 许可证

此项目根据 MIT 许可证获得许可-有关详细信息，请参见[LICENSE](LICENSE)文件。

---
