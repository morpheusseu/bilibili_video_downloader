# Bilibili 视频 / 音频下载器

基于 **bpi-py 0.3.1** 的独立 PyQt5 桌面应用；也提供共享同一下载引擎的 CLI。不依赖 Boxy。

## 功能

| 输入 | 能力 |
| --- | --- |
| BV / AV ID、视频链接 | 指定分 P、链接 `?p=2`，或下载全部分 P |
| 收藏夹 ID、包含 `fid=` 的链接 | 按服务器 `has_more` 分页，去重后批量下载 |
| UID + 收藏夹名称正则 | 匹配一个或多个收藏夹；空正则匹配全部；跨收藏夹去重 |
| `ep123`、番剧单集链接 | 使用 bpi-py 的番剧元数据和播放接口，不抓网页字符串 |
| 输出 | MP4 音视频合并；只下载音频并转 MP3 / WAV；支持 DASH 和分段合并 |
| 登录 | 真实 Chrome 中的官方登录页面；手动扫码、密码或平台要求的验证 |

界面支持不下载媒体的内容列表预览、输出目录选择、实时字节进度、取消、打开输出目录。
下载/转换在独立线程的 asyncio 循环里运行，不阻塞界面。主要操作按钮固定显示。
公开内容无需登录。仅下载当前权限允许的内容；不绕过 VIP、地区限制、DRM 或风控。

## 界面与使用流程

1. 在“内容来源”粘贴链接或 ID，选择单个或全部分 P；可先“检查来源”查看前 10 个分 P。
2. 在“保存设置”选择 MP4 / MP3 / WAV 和目录。音频格式不需要选择画质，已有文件不会覆盖。
3. 点击“开始下载”；底部任务区始终显示进度和取消入口，完成后可打开文件夹。
4. 需要账号权限时，展开右上角“账号与登录”。Cookie、FFmpeg 路径和操作详情按需展开。

整体界面使用单一蓝色、系统字体、浅色卡片与清楚的文字层级，依据用户指定的
[Apple 设计参考](design/README.md)适配原生桌面工具，不引入远程字体、WebView 或装饰性动画。
支持最小 760×650 窗口；表单可以滚动，主要操作保持固定。改变来源后旧预览会清除，
不会在打字、切换设置或展开面板时自动访问 Bilibili。

## 安装和运行

需要 Python 3.11+、[uv](https://docs.astral.sh/uv/)。账号登录额外需要已安装的 **Google Chrome**。

```sh
uv sync --frozen
uv run --frozen bili-downloader
# 等价的源码入口：
uv run --frozen python main_window.py
```

FFmpeg 优先使用用户指定路径，其次系统 PATH，否则使用 `imageio-ffmpeg` wheel 自带的二进制。
无需为了首次运行单独配置 FFmpeg。可在界面的高级设置里覆盖路径。

也可在仓库根目录执行 `pip install -r requirements.txt`；依赖定义只维护在 `pyproject.toml`，
完整可重复环境以 `uv.lock` 为准。

## 登录与隐私

1. 展开右上角 **账号与登录**，点击 **在 Chrome 中登录**。
   应用只启动自己拥有的临时浏览器会话，不读取已有 Chrome profile。
2. 在 Bilibili 官方页面完成登录。程序不填写密码、不处理验证码或 CAPTCHA。
3. 读取该临时会话的 Bilibili 登录/设备 Cookie 和真实浏览器 User-Agent，由 bpi-py 验证账号身份。
4. 登录完成后关闭临时 Chrome、清理其临时 profile，并回到下载器。后续 API 使用同一浏览器 UA。

这是实际 Chrome 登录，保留浏览器真实产生的设备 Cookie，不制造设备指纹，不模拟 Android 设备。
**Bilibili 最终显示的设备名称由平台决定，本应用不能保证它一定显示为 Chrome。**
浏览器自身加载官方网页的资源请求由该网页管理，不受下面的数据 API 请求节奏控制。

默认只保留当前进程中的登录。只有用户勾选 **记住登录**，才保存到系统加密凭据存储：
macOS Keychain / Windows Credential Manager / Linux Secret Service。
没有可用的原生后端时明确报错，**不降级到明文文件**。
使用保存的登录需要用户点击“读取钥匙串登录”；启动时不自动读取钥匙串或发账号请求。
“退出并忘记”清除本应用的内存和保存凭证，不注销其他浏览器/手机客户端。
若登录有效但钥匙串保存失败，会保留本次内存登录并明确提示保存失败，不显示无条件成功。

高级 Cookie 输入仍然可用：只在内存中传给 bpi-py，不进入设置、终端参数、日志或下载 URL。
本版本不读取旧 `configuration/passport.json`，也不自动导入旧明文凭证。
旧仓库历史中的凭证文件没有被改写历史；若其中曾保存仍有效的真实凭证，应在官方客户端撤销。

## 请求节奏和失败处理

- 同一个应用实例的 API 和媒体请求共用一把串行锁；从上一请求**完整结束**到下一请求开始，
  随机等待 **0.5～1.5 秒**，启动后的第一个请求也等待。
- 借鉴 WhatsApp runtime 的 jitter / 防重试风暴原则，但不把其重连指数退避直接套在正常下载上。
- 不建立后台账号心跳，不轮询收藏/动态，不并行抓取，不在每个输入变动时自动请求。
- HTTP 403/412/429、API `-352/-412` 会停止本实例后续请求；请到官方客户端检查账号，
  遵守平台要求后再重启。没有自动重试、换代理、换账号或伪造设备的逻辑。
- CDN 的正常 HTTPS 重定向逐跳验证域名，至多四次请求，也受随机间隔约束。
  不跟随任意网站、登录页或 HTTP 降级；媒体客户端不携带 Bilibili 登录 Cookie。
- 节奏控制**不能保证账号不被限制**。不要同时运行多个实例，也不要把批量下载当作不限量授权。

收藏夹扫描以实际分页结果而非数量统计决定是否继续。空页/重复页却仍宣称 `has_more`
会报错停止，不把不完整列表当作成功。没有 BVID 或非视频的收藏条目不会交给视频下载器。

媒体按固定大小流式写入独立临时目录，不把大视频载入内存；不需要先整段下载一遍探测长度。
完成转换后才原子发布最终文件。文件名包含 BV/CID，避免同名视频和不同分 P 冲突。
已有非空文件跳过，空文件、目录、链接会提示处理，**不覆盖用户已有文件**。
取消或失败会清理该任务的临时文件；已完成文件保留，重新运行会跳过它们。
FFmpeg 通过参数数组运行，不使用 `shell=True`。

## CLI

```sh
# 只检查元数据；不会下载媒体，默认预览最多 20 个分 P
uv run --frozen bili-download inspect BV178411i7AD
uv run --frozen bili-download inspect 'https://www.bilibili.com/video/BV178411i7AD?p=1'

# 输出到指定目录
uv run --frozen bili-download download BV178411i7AD --format mp4 --quality 16 --output ./downloads
uv run --frozen bili-download download BV178411i7AD --format mp3 --all-parts --output ./downloads
uv run --frozen bili-download download 123456 --mode favorite --format wav
uv run --frozen bili-download download 398421039 --mode user --pattern 'piano.*' --format mp3
uv run --frozen bili-download inspect ep746940

# 只有显式要求时才保存/读取系统登录
uv run --frozen bili-download login --remember
uv run --frozen bili-download download BV178411i7AD --use-saved-login
```

分 P 从 **1** 开始；`--all-parts` 优先于单个分 P。画质受账号和实际返回流约束，不保证请求到 1080P
就获得 1080P；视频流选择不会高于所选画质。输入必须是明确支持的 ID / bilibili.com 完整链接，
不自动展开 `b23.tv` 短链接或从任意粘贴文字中猜测目标。

## 验证与打包

```sh
uv run --frozen pytest -q
uv run --frozen ruff check .
uv run --frozen ruff format --check .
```

自动测试只用合成数据和 MockTransport，不读取账号、钥匙串或访问 Bilibili。
覆盖真实 bpi-py 解析、分页去重/不前进、分 P、原生凭证存储边界、Chrome 登录交接/取消、
真实 FFmpeg 的三种输出、分段合并、下载取消/不完整文件、CDN 跳转、风控锁停及 Qt 线程/界面恢复。
UI 另覆盖来源预览失效、格式/分 P 控件语义、完整参数传递、完成进度保留、窄窗口取消可达，
以及钥匙串失败不被成功状态覆盖；离线测试不使用真实账号。

手动真实验收记录（2026-10-21）：

- 旧项目演示视频 `BV178411i7AD`：公开元数据、360P MP4 下载/合并、MP3 和 WAV 下载/转换成功。
- MP3 / WAV 均经过 FFmpeg 完整解码；同一会话的 7 次请求最小间隔实测约 0.620 秒。
- 公开收藏夹额外做了匿名真实验证：一个 91 条内容的收藏夹，以每页 1 条读取两页，均非空且无重复。
  共 3 次请求，最小间隔约 0.753 秒；没有登录其所有者账号或扫描/下载整个收藏夹。
- macOS `.app` 已实际启动，来源预览、独立 Chrome 官方登录页启动/取消、GUI 下载与合并均通过。
- 本应用只实现原下载器的功能，不自动同步动态、历史或私信。此前独立 bpi-py 只读实验
  已确认关注动态和普通私信可用；本应用的多 P 真实批量下载仍待样本验收。
- `ep746940` 的真实番剧元数据可解析，但其下载权限标记为禁止，程序正确拒绝；
  允许下载的番剧、带权限账号下载、真实 Chrome 登录后的设备标签、原生钥匙串弹窗仍需人工验收。
- Windows/Linux 上的界面、钥匙串和最终分发包未在本机验证。

Windows 打包：`./pyinstaller.ps1`，需要调试控制台时 `./pyinstaller.ps1 -Console`。
其他平台使用同一条命令：

```sh
uv run --frozen --group build pyinstaller --noconfirm --clean --onedir --windowed \
  --name BilibiliDownloader --collect-data imageio_ffmpeg --collect-data bili_downloader \
  --collect-all playwright main_window.py
```

分发包不包含 Chrome，也不下载或替换用户的系统浏览器。PyQt5、FFmpeg 和其他依赖各有许可要求，
分发时应保留并检查其许可；bpi-py 的 MIT 许可不等于 Bilibili 对接口使用或媒体下载的授权。
