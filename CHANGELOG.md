#本次更新#
1. 修复了QQ Linux deb 下载源404错误
2. 版本选择器移到「配置」弹窗内的「版本管理」分组，替换为自定义玻璃风下拉（带搜索框）
3. 安装新版本前自动清理旧版本文件（保留 config 配置目录），不再需要单独的卸载按钮
4. 所有危险操作（停止/启动/同步/安装/重启/清理/保存配置）改为插件内置弹窗确认，不再依赖浏览器原生 confirm
5. 移除了每次启动自动检测并自动下载安装 NapCat 的逻辑，改为由用户在网页手动选择版本后安装
6. 启动和安装时自动 apt install 全套 Linux 运行依赖（libatspi2.0-0 libgtk-3-0 libgbm1 libasound2t64 libnss3 libnotify4 libsecret-1-0 libxss1 libxtst6 libxkbfile1 xvfb x11vnc openbox dbus-user-session fonts-wqy-zenhei g++）
