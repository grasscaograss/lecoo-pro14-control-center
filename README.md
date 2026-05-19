# Lecoo Pro14 Control Center

## English

### About

This project is based on [LaVashikk/Lecoo-Control-Center](https://github.com/LaVashikk/Lecoo-Control-Center).

Original project:

- Author: LaVashikk
- Repository: https://github.com/LaVashikk/Lecoo-Control-Center
- License: MIT

This repository adds a Windows GUI package, service repair/recovery helpers, fan curve controls, and startup-delay handling for Lecoo Pro14 usage.

The bundled service installer and uninstaller are kept aligned with the upstream 0.4.0 Windows release. Project-specific service recovery behavior is handled by `repair-service-recovery.bat` and the GUI launcher code instead of modifying the upstream installer.

### Download

End users should download packaged builds from GitHub Releases:

https://github.com/grasscaograss/lecoo-pro14-control-center/releases

Compiled `.exe`, `.dll`, `.7z`, and `.zip` files are release artifacts and are not tracked in the source repository.

### Source Layout

- `GUI.py`: PyQt5 GUI source code.
- `来酷pro14控制中心.spec`: PyInstaller build configuration.
- `install.bat`: Upstream 0.4.0 installer for the background service.
- `repair-service-recovery.bat`: Repairs the background service auto-restart policy.
- `uninstall.bat`: Upstream 0.4.0 uninstaller for the background service.
- `NOTICE.md`: Upstream attribution and project notice.

### Build

Install Python dependencies:

```powershell
python -m pip install -r requirements.txt
```

Before building the full packaged app, place these runtime files next to `GUI.py`:

- `lecoo-ec-daemon.exe`
- `lecoo-ctrl.exe`
- `inpoutx64.dll`

They are not tracked in this source repository. Use the upstream build outputs or copy them from a release package.

Build the Windows executable:

```powershell
python -m PyInstaller --clean -y "来酷pro14控制中心.spec"
```

The packaged executable is generated under `dist/`.

### Usage

1. Download the latest release package.
2. Keep all files in the package folder together. Do not copy only the `.bat` scripts by themselves.
3. Double-click `来酷pro14控制中心.exe` to start the app.
4. On first launch, or when the background service is abnormal, Windows may show an administrator permission prompt. Choose **Yes**.
5. Auto-start waits 10 seconds after login before launching, so Windows desktop scaling and DPI state can settle first.

### Fan Curve

- Click **打开风扇曲线设置** in the main window.
- **升速延迟** controls how long the app waits before increasing fan speed after temperature rises.
- **降速延迟** controls how long the app waits before decreasing fan speed after temperature drops.

### Logs

```text
%LOCALAPPDATA%\LecooControlCenter\logs
```

### License

This project keeps the original MIT license notice from LaVashikk/Lecoo-Control-Center and includes copyright information for local modifications.

Third-party dependencies and binary release contents may have their own licenses.

## 中文

### 项目说明

本项目基于 [LaVashikk/Lecoo-Control-Center](https://github.com/LaVashikk/Lecoo-Control-Center) 开发。

原项目信息：

- 作者：LaVashikk
- 仓库：https://github.com/LaVashikk/Lecoo-Control-Center
- 许可证：MIT

本仓库在原项目基础上增加了 Windows GUI 打包、后台服务修复/恢复脚本、风扇曲线控制、开机自启延迟等内容，用于 Lecoo Pro14。

服务安装和卸载脚本保持与上游 0.4.0 Windows release 一致。项目自己的服务恢复策略由 `repair-service-recovery.bat` 和 GUI 启动逻辑处理，不再改动上游安装脚本。

### 下载

普通用户请从 GitHub Releases 下载打包好的版本：

https://github.com/grasscaograss/lecoo-pro14-control-center/releases

编译后的 `.exe`、`.dll`、`.7z`、`.zip` 属于发布产物，不放在源码仓库里。

### 源码结构

- `GUI.py`：PyQt5 GUI 源码。
- `来酷pro14控制中心.spec`：PyInstaller 打包配置。
- `install.bat`：上游 0.4.0 后台服务安装脚本。
- `repair-service-recovery.bat`：修复后台服务自动重启策略。
- `uninstall.bat`：上游 0.4.0 后台服务卸载脚本。
- `NOTICE.md`：上游来源和项目说明。

### 构建

安装 Python 依赖：

```powershell
python -m pip install -r requirements.txt
```

完整打包前，需要把下面这些运行时文件放到 `GUI.py` 同级目录：

- `lecoo-ec-daemon.exe`
- `lecoo-ctrl.exe`
- `inpoutx64.dll`

这些文件不放在源码仓库中。可以使用上游构建产物，或者从 Release 包里复制。

构建 Windows 可执行文件：

```powershell
python -m PyInstaller --clean -y "来酷pro14控制中心.spec"
```

打包后的可执行文件会生成在 `dist/` 目录。

### 使用

1. 下载最新 Release 包。
2. 保持包内文件放在一起，不要只单独拷贝 `.bat` 脚本。
3. 双击运行 `来酷pro14控制中心.exe`。
4. 首次运行或后台服务异常时，Windows 可能会弹出管理员授权，请选择 **是**。
5. 开机自启会延迟 10 秒启动，用于等待 Windows 桌面缩放和 DPI 状态稳定。

### 风扇曲线

- 在主界面点击 **打开风扇曲线设置**。
- **升速延迟** 控制温度升高后等待多久再提高风扇转速。
- **降速延迟** 控制温度降低后等待多久再降低风扇转速。

### 日志位置

```text
%LOCALAPPDATA%\LecooControlCenter\logs
```

### 许可证

本项目保留 LaVashikk/Lecoo-Control-Center 原项目的 MIT 许可证声明，并补充本地修改部分的版权信息。

第三方依赖和 Release 包内的二进制内容可能有各自的许可证。
