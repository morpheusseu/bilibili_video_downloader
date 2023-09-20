pyinstaller main_window.py --add-data="configuration;configuration" --add-data="bilibili_api;bilibili_api"  --add-data="github.png;." --add-data="bin;bin" --windowed -F
.\dist\main_window.exe
Pause