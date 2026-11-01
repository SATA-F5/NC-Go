# 7.5.0
1. 版本号更新为 7.5.0
2. 保存 sudo 密码时增加显式二次确认；LinuxQQ 版本不兼容时，启动、重启、安装前提示可能自动重装 QQ 及数据丢失风险
3. README 补充 LinuxQQ deb 来源说明，明确其为官方安装包的固定兼容版本备份

# 7.4.2
1. QQ Linux deb 下载源固定为 SATA-F5/NC-Go 仓库（QQ_3.2.34_Linux_amd64.deb），保留多镜像加速回退
2. 版本选择器移到「配置」弹窗内的「版本管理」分组，替换为自定义玻璃风下拉（带搜索框）
3. 安装新版本前自动清理旧版本文件（保留 config 配置目录），不再需要单独的卸载按钮
4. 所有危险操作（停止/启动/同步/安装/重启/清理/保存配置）改为插件内置弹窗确认，不再依赖浏览器原生 confirm
5. 移除了每次启动自动检测并自动下载安装 NapCat 的逻辑，改为由用户在网页手动选择版本后安装
6. 启动和安装时自动 apt install 全套 Linux 运行依赖（libatspi2.0-0 libgtk-3-0 libgbm1 libasound2t64 libnss3 libnotify4 libsecret-1-0 libxss1 libxtst6 libxkbfile1 xvfb x11vnc openbox dbus-user-session fonts-wqy-zenhei）
7. NapCat 运行中禁止安装新版本（前后端双重拦截，弹提示要求先停止）
8. 新增后端 API：GET /releases；POST /install 支持传入指定版本
9. launcher.sh 过滤 QQ 的 GLX/EGL GPU 噪音日志，日志面板只保留 NapCat 有效输出
10. Linux 原生安装改为与官方 NapCat-Installer 一致：写入 QQ 的 `resources/app/loadNapCat.js` 并把 `package.json` 的 `main` 指向它，不再下载/编译 launcher.cpp，也不再使用 LD_PRELOAD 注入
11. 删除 `.so` 硬编码路径带来的 `napcat/napcat.mjs` 软链接、`NAPCAT_BOOTMAIN` 补丁等重复兼容代码；启动统一改用 `xvfb-run` + 24 位虚拟屏 + `--disable-gpu`
12. 卸载 NapCat 时自动还原 QQ 的 `package.json` 并删除 `loadNapCat.js`
13. WebUI 全面改版为 SnowLuma-Go 同款玻璃拟态控制台：左侧状态/操作，右侧实时日志，配置与下载拆分为独立弹窗
14. sudo 体系统一：全部命令走 `linux_tools.run_sudo(-S -p "")`；WebUI 用 RSA 公钥加密密码，后端一次性解密，不再写入配置或日志
15. 配置区新增「界面」浅色模式左右拨动开关：默认跟随 AstrBot 主题，拨动后固定为所选主题，立即生效并记忆
16. 下载页新增内置镜像下拉、版本列表、即时保存下载设置；配置页新增 Linux 依赖安装入口
17. 插件显示名统一为「NapCat Go」；版本号更新为 7.4.2
18. 修复浅色模式主按钮白底白字、标题颜色和整体背景；浅色统一为 AstrBot 风格白卡片 + 浅灰背景 + 黑字
19. 首次引导扩展为 23 步：欢迎、创建 AstrBot 机器人、关闭 SSL、完整界面导览和最终提醒；支持配置里「使用引导」重播
20. 引导完成状态增加版本号，旧版本已跳过引导的用户升级后会重新看到一次新版引导
21. 配置新增「NapCat WebUI 端口」，保存后写入 webui.json；移除「服务器浏览器打开 WebUI」按钮
22. 配置区「界面」改为与 SnowLuma Go 一致的左右拨动开关，主题选择保存到插件配置（ui_theme），支持恢复跟随 AstrBot
23. 自定义页面整体对齐 SnowLuma Go 的布局与浅色 / 深色样式；插件显示名与页面标题统一为「NapCat Go」
24. 自动检测到 NapCat 未安装时，主按钮改为「安装 NapCat」并直接打开下载与安装，不再执行无效的启动
25. 安装 / 更新完成后默认自动启动 NapCat（可在配置中关闭），启动接口增加未安装防护并引导到下载页
26. 恢复 7.4.1 的配置页 sudo 密码保存：在「运行方式」填写一次，经本机密钥加密落盘；启动、安装、同步、清理等操作自动提权，不再反复弹出 sudo 输入框
