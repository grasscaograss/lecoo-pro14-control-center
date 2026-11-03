基于上游 **0.3.3** 的 IPC 修复版，内核与通信协议保持 0.3.3。

- 将 `interprocess` 固定为 2.4.2，修复 Windows 客户端断开时触发 `0xc0000409`、导致整个内核退出的问题。
- 保留原 GUI、风扇曲线逻辑、充电限制和开机自启行为。
- 恢复 0.3.3 配套的安装脚本。
- Windows Release 回归通过：192 次断连场景、256 轮正常收发。

退出旧版托盘程序，解压完整新包，以管理员身份运行 `install.bat`，再启动 GUI。配置保存在用户目录中；更换程序目录后请重新设置开机自启。

附件中的 `SHA256SUMS.txt` 用于核对下载包。完整安装与构建说明见 [README](https://github.com/grasscaograss/lecoo-pro14-control-center#readme)。

上游问题与修复：[interprocess #93](https://github.com/kotauskas/interprocess/issues/93)、[aa9dedc](https://github.com/kotauskas/interprocess/commit/aa9dedca30d87699e020a1e7177acb200aaf0b3e)。
