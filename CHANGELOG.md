#本次更新#
1. QQ Linux deb 下载源固定为 SATA-F5/NC_Go 仓库（QQ_3.2.34_Linux_amd64.deb），保留多镜像加速回退
2. 版本选择器移到「配置」弹窗内的「版本管理」分组，替换为自定义玻璃风下拉（带搜索框）
3. 安装新版本前自动清理旧版本文件（保留 config 配置目录），不再需要单独的卸载按钮
4. 所有危险操作（停止/启动/同步/安装/重启/清理/保存配置）改为插件内置弹窗确认，不再依赖浏览器原生 confirm
5. 移除了每次启动自动检测并自动下载安装 NapCat 的逻辑，改为由用户在网页手动选择版本后安装
6. 启动和安装时自动 apt install 全套 Linux 运行依赖（libatspi2.0-0 libgtk-3-0 libgbm1 libasound2t64 libnss3 libnotify4 libsecret-1-0 libxss1 libxtst6 libxkbfile1 xvfb x11vnc openbox dbus-user-session fonts-wqy-zenhei g++）
7. NapCat 运行中禁止安装新版本（前后端双重拦截，弹提示要求先停止）
8. 新增后端 API：GET /releases；POST /install 支持传入指定版本
9. launcher.sh 过滤 QQ 的 GLX/EGL GPU 噪音日志，日志面板只保留 NapCat 有效输出
10. Linux 原生安装改为与官方 NapCat-Installer 一致：写入 QQ 的 `resources/app/loadNapCat.js` 并把 `package.json` 的 `main` 指向它，不再下载/编译 launcher.cpp，也不再使用 LD_PRELOAD 注入
11. 删除 `.so` 硬编码路径带来的 `napcat/napcat.mjs` 软链接、`NAPCAT_BOOTMAIN` 补丁等重复兼容代码；启动统一改用 `xvfb-run` + 24 位虚拟屏 + `--disable-gpu`
12. 卸载 NapCat 时自动还原 QQ 的 `package.json` 并删除 `loadNapCat.js`
