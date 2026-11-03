# Lecoo Pro14 Control Center

来酷 Pro14 Windows 控制中心：自定义风扇曲线、性能档位、充电限制、灯光和开机自启。

## 当前版本

**[下载 0.3.3 IPC 修复版](https://github.com/grasscaograss/lecoo-pro14-control-center/releases/latest)**

| 组件 | 基线 |
| --- | --- |
| EC 内核与 IPC 协议 | 0.3.3 |
| `interprocess` | 固定为 2.4.2 |
| GUI | 保留现用版本及风扇曲线逻辑 |
| Rust 构建工具链 | 1.94.0 |

本版修复客户端断开连接时内核以 `0xc0000409` 退出的问题。根因及上游修复见 [interprocess #93](https://github.com/kotauskas/interprocess/issues/93) 和 [aa9dedc](https://github.com/kotauskas/interprocess/commit/aa9dedca30d87699e020a1e7177acb200aaf0b3e)。

Windows Release 回归覆盖 192 次断连场景和 256 轮正常收发。现用设备已完成内核替换、GUI 重连和温度/风扇读取检查；长期运行仍需持续验证。

## 安装与更新

1. 从 Releases 下载完整 ZIP，并用 `SHA256SUMS.txt` 核对校验值。
2. 更新前先退出旧版托盘程序，再解压完整新包。
3. 右键 `install.bat`，选择“以管理员身份运行”。
4. 运行 `来酷pro14控制中心.exe`。首次安装或服务异常时，按提示允许管理员修复。
5. 如果更换了程序目录，重新设置“开机自启”。自启会在登录后延迟 10 秒运行。

保持包内文件放在同一目录；内核、CLI 和 DLL 使用本仓库整包中的配套版本。安装程序保留用户配置和内核状态。

```powershell
Get-FileHash .\lecoo-pro14-control-center-0.3.3-ipcfix.1-windows-x64.zip -Algorithm SHA256
```

## 风扇曲线与配置

- 在主界面点击“打开风扇曲线设置”，启用自定义曲线并调整温度/转速点。
- “升速延迟”和“降速延迟”分别控制持续升温、降温后的响应等待时间。
- 自定义曲线由 GUI 执行。需要持续动态调速时，将程序最小化到托盘；退出 GUI 后，内核保留最后下发的风扇设置。

| 内容 | 路径 |
| --- | --- |
| GUI 配置 | `%LOCALAPPDATA%\LecooControlCenter\config.json` |
| GUI、安装和修复日志 | `%LOCALAPPDATA%\LecooControlCenter\logs` |
| 内核状态和日志 | `%PROGRAMDATA%\LecooControl` |

`repair-service-recovery.bat` 用于修复后台服务自动重启策略；`uninstall.bat` 用于卸载服务。

## 源码与构建

本项目基于 [LaVashikk/Lecoo-Control-Center](https://github.com/LaVashikk/Lecoo-Control-Center)，保留原项目的 MIT 许可证与署名。

| 文件 | 用途 |
| --- | --- |
| `GUI.py`、`来酷pro14控制中心.spec` | PyQt5 GUI 与 PyInstaller 打包配置 |
| `core/0.3.3-ipcfix.patch` | 0.3.3 内核依赖补丁及 Windows 断连回归测试 |
| `scripts/build-core.ps1` | 拉取固定上游提交，应用补丁，测试并构建内核/CLI |
| `scripts/release.py`、`release/config.json` | 校验、打包及 GitHub Release API 发布 |
| `.github/workflows/release.yml` | Windows 构建、回归测试和发布流程 |

在 Windows x64 上安装 Git、Visual Studio C++ Build Tools 和 Rust 后执行：

```powershell
rustup toolchain install 1.94.0 --profile minimal
.\scripts\build-core.ps1
```

脚本固定上游提交 `dd850af1f15110deed00b9e3b7546b5fa8b3ea41`，使用锁文件构建。产物位于 `dist/runtime/`，内核版本仍为 0.3.3。

需要自行重建 GUI 时，将上述内核、CLI 和 DLL 放在 `GUI.py` 同级目录，再执行：

```powershell
python -m pip install -r requirements.txt
python -m PyInstaller --clean -y "来酷pro14控制中心.spec"
```

正式发布沿用已校验的 GUI 二进制，仅重建修复后的内核和 CLI；每个安装包包含 `version.json`，记录来源、构建版本和组件哈希。二进制与完整 ZIP 通过 Releases 分发，不提交到 Git 源码中。

## English

Windows control center for Lecoo Pro14, based on [LaVashikk/Lecoo-Control-Center](https://github.com/LaVashikk/Lecoo-Control-Center).

The current release keeps the **0.3.3 core and IPC protocol**, pins `interprocess` to **2.4.2**, and fixes process aborts when a Windows pipe client disconnects. The existing GUI and fan curve behavior are retained. Windows Release tests cover 192 disconnect scenarios and 256 normal request/reply cycles.

Download the complete ZIP from [Releases](https://github.com/grasscaograss/lecoo-pro14-control-center/releases/latest), verify `SHA256SUMS.txt`, exit the old tray app, and run `install.bat` as administrator before opening the GUI. Keep all package files together. If its directory changes, reconfigure auto-start. User settings are preserved.

Custom fan curves run in the GUI: keep it in the tray for continuous control. Exiting the GUI leaves the last fan setting active in the core. Settings and GUI logs are under `%LOCALAPPDATA%\LecooControlCenter`; core state and logs are under `%PROGRAMDATA%\LecooControl`.

To build the runtime, install Rust 1.94.0 and the Windows C++ tools, then run `scripts/build-core.ps1`. The script checks out the pinned 0.3.3 source, applies the patch, runs the Windows regression tests, and builds the runtime under `dist/runtime/`. Release publishing uses GitHub Actions and the GitHub REST API.

The original MIT license is retained. Third-party runtime dependencies have their own licenses; see `NOTICE.md`.
