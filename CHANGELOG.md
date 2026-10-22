#本次更新#
1. QQ Linux deb 下载源固定为 SATA-F5/NC_Go 仓库（QQ_3.2.34_Linux_amd64.deb），保留多镜像加速回退
2. 网页主界面新增版本选择下拉框（调用 GitHub Releases API 拉取 NapCat 版本列表）
3. 网页主界面新增「安装所选版本」和「卸载当前版本」按钮
4. 移除了每次启动自动检测并自动下载安装 NapCat 的逻辑，改为由用户在网页手动选择版本后安装
5. 启动时保留 QQ 和系统依赖（g++、Xvfb）的自动检测与自动安装（不兼容 QQ 自动 purge 后装推荐版）
6. 新增后端 API：GET /releases、POST /uninstall；POST /install 支持传入指定版本
