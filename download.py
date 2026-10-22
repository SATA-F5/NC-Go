import asyncio
import json
import os
import shutil
import subprocess
import time
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import aiohttp

from astrbot.api import logger

from .utils import (
    IS_WINDOWS, IS_LINUX,
    NAPCAT_RELEASE_BASE, NAPCAT_LINUX_LAUNCHER_CPP,
    DEFAULT_NAPCAT_VERSION,
    NAPCAT_MIRROR_CANDIDATES,
    LINUXQQ_DEB_URLS,
    NAPCAT_SUPPORTED_QQ_MIN, NAPCAT_SUPPORTED_QQ_MAX,
    RECOMMENDED_LINUXQQ_VERSION,
    make_ssl_context,
    get_linux_qq_version, get_linux_qq_build_code,
    is_linux_qq_version_compatible, is_qq_installed,
    is_deb_build_compatible,
    DOCKER_WEBUI_PORT,
)


class DownloadMixin:
    """提供 NapCat / LinuxQQ 的下载与安装能力。

    依赖宿主类的以下属性/方法：
    - self.plugin            : NapCatGoPlugin
    - self.napcat_dir        : Path
    - self.plugin_dir        : Path
    - self.log_lines         : deque
    - self.download_state    : dict
    - self._reset_download_state()
    - self._is_new_style_install(d)
    - self._find_entry()
    - self._find_new_style_root()
    - self._sudo_warm_async(pw)
    """

    # ---------- 通用下载 ----------

    async def _download_file(self, url: str, dest: Path,
                             connect_timeout: int = 15, read_timeout: int = 60,
                             progress_label: str = "",
                             progress_index: int = 0,
                             progress_total: int = 0,
                             max_time: int = 900) -> bool:
        try:
            ssl_ctx = make_ssl_context(strict=False)
            connector = aiohttp.TCPConnector(ssl=ssl_ctx)
            async with aiohttp.ClientSession(connector=connector) as session:
                async with session.get(
                    url,
                    timeout=aiohttp.ClientTimeout(
                        total=max_time, sock_connect=connect_timeout,
                        sock_read=read_timeout),
                ) as resp:
                    if resp.status != 200:
                        self.log_lines.append(f"[qq-install] {progress_label} HTTP {resp.status}")
                        return False
                    total = resp.content_length or 0
                    got = 0
                    t0 = time.time()
                    with open(dest, "wb") as f:
                        async for chunk in resp.content.iter_chunked(1024 * 64):
                            f.write(chunk)
                            got += len(chunk)
                            elapsed = time.time() - t0
                            if elapsed > 0:
                                self.download_state["downloaded_mb"] = got / 1024 / 1024
                                self.download_state["speed_kbps"] = got / elapsed / 1024
                                if total > 0:
                                    self.download_state["total_mb"] = total / 1024 / 1024
                                    self.download_state["percent"] = min(100, got * 100 // total)
                                else:
                                    self.download_state["total_mb"] = 0.0
                                self.download_state["current_mirror"] = progress_label
                                self.download_state["current_index"] = progress_index
                                self.download_state["total_mirrors"] = progress_total
                    return True
        except Exception as e:
            self.log_lines.append(f"[qq-install] {progress_label} 异常: {e}")
            return False

    async def _validate_deb(self, path: Path) -> bool:
        try:
            with open(path, "rb") as f:
                magic = f.read(7)
            return magic == b"!<arch>"
        except Exception:
            return False

    # ---------- 版本解析与完整性校验 ----------

    @staticmethod
    def _sha256_file(path: Path) -> str:
        import hashlib
        h = hashlib.sha256()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(1024 * 256), b""):
                h.update(chunk)
        return h.hexdigest()

    async def _resolve_napcat_release(self, version: str) -> Dict[str, Any]:
        """通过 GitHub API 解析 NapCat 版本，返回 {tag, zip_url, sha256}。

        - version == "latest"：查 /releases/latest 取 tag_name。
        - 否则按指定 tag 查 /releases/tags/<tag>。
        - 同时在 release assets 里找 .sha256 / SHA256SUMS 文件，作为校验基准。
        - API 不可达时回退到 releases/latest/download/ 直链（无 sha256 基准）。
        """
        result: Dict[str, Any] = {"tag": version, "zip_url": "", "sha256": None}
        api_base = "https://api.github.com/repos/NapNeko/NapCatQQ"
        if version == "latest":
            api_url = f"{api_base}/releases/latest"
        else:
            api_url = f"{api_base}/releases/tags/{version}"

        ssl_ctx = make_ssl_context(strict=True)
        try:
            connector = aiohttp.TCPConnector(ssl=ssl_ctx)
            async with aiohttp.ClientSession(connector=connector) as session:
                async with session.get(
                    api_url,
                    timeout=aiohttp.ClientTimeout(total=15, sock_connect=8),
                    headers={"Accept": "application/vnd.github+json",
                             "User-Agent": "napcat-go-plugin"},
                ) as resp:
                    if resp.status != 200:
                        raise RuntimeError(f"GitHub API HTTP {resp.status}")
                    data = await resp.json()
        except Exception as e:
            self.log_lines.append(f"[download] ⚠ GitHub API 解析版本失败({e})，回退 latest 直链")
            result["tag"] = "latest"
            result["zip_url"] = f"https://github.com/NapNeko/NapCatQQ/releases/latest/download/NapCat.Shell.zip"
            return result

        tag = data.get("tag_name") or version
        result["tag"] = tag
        result["zip_url"] = f"https://github.com/NapNeko/NapCatQQ/releases/download/{tag}/NapCat.Shell.zip"

        # 在 assets 里找 checksum
        assets = data.get("assets") or []
        checksum_url = None
        for a in assets:
            name = (a.get("name") or "").lower()
            if "napcat.shell.zip" in name and (name.endswith(".sha256") or "sha256" in name):
                checksum_url = a.get("browser_download_url")
                break
            if name in ("sha256sums", "sha256sums.txt", "checksums.sha256"):
                checksum_url = a.get("browser_download_url")
                break
        if checksum_url:
            try:
                async with aiohttp.ClientSession(connector=connector) as session2:
                    async with session2.get(
                        checksum_url,
                        timeout=aiohttp.ClientTimeout(total=15, sock_connect=8),
                    ) as r:
                        if r.status == 200:
                            text = (await r.text()).strip()
                            # 形如 "<hash>  NapCat.Shell.zip"
                            for line in text.splitlines():
                                line = line.strip()
                                if not line:
                                    continue
                                parts = line.split()
                                if len(parts) >= 1 and len(parts[0]) == 64:
                                    result["sha256"] = parts[0].lower()
                                    break
            except Exception as e:
                self.log_lines.append(f"[download] ⚠ 获取 checksum 失败({e})，跳过哈希校验")
        return result

    async def list_napcat_releases(self) -> List[Dict[str, Any]]:
        """通过 GitHub API 列出 NapCat 可下载版本（最近 30 条）。

        返回 [{tag, name, published_at, prerelease}, ...]，失败返回空列表。
        """
        api_base = "https://api.github.com/repos/NapNeko/NapCatQQ"
        ssl_ctx = make_ssl_context(strict=True)
        try:
            connector = aiohttp.TCPConnector(ssl=ssl_ctx)
            async with aiohttp.ClientSession(connector=connector) as session:
                async with session.get(
                    f"{api_base}/releases?per_page=30",
                    timeout=aiohttp.ClientTimeout(total=20, sock_connect=8),
                    headers={"Accept": "application/vnd.github+json",
                             "User-Agent": "napcat-go-plugin"},
                ) as resp:
                    if resp.status != 200:
                        self.log_lines.append(f"[releases] GitHub API HTTP {resp.status}")
                        return []
                    data = await resp.json()
        except Exception as e:
            self.log_lines.append(f"[releases] 拉取版本列表失败: {e}")
            return []

        out: List[Dict[str, Any]] = []
        for rel in data:
            if not isinstance(rel, dict):
                continue
            tag = (rel.get("tag_name") or "").strip()
            if not tag:
                continue
            out.append({
                "tag": tag,
                "name": (rel.get("name") or tag).strip(),
                "published_at": rel.get("published_at") or "",
                "prerelease": bool(rel.get("prerelease")),
            })
        return out

    # ---------- NapCat 下载 / 解压（Windows + Linux 共用） ----------

    # 与 Ubuntu 实测方案完全一致的 launcher.sh（LD_PRELOAD + Xvfb :1）
    LAUNCHER_SH_CONTENT = """#!/bin/bash
Xvfb :1 -screen 0 1x1x8 +extension GLX +render > /dev/null 2>&1 &
export DISPLAY=:1
trap "" SIGPIPE
LD_PRELOAD=./libnapcat_launcher.so qq --no-sandbox
"""

    # .so 的 constructor 也会生成此文件，这里预生成一份保证就位
    LOAD_NAPCAT_JS_CONTENT = (
        "const path = require('path');"
        "const CurrentPath = process.env.NAPCAT_BOOTMAIN || path.dirname(__filename);"
        "(async () => {"
        "    await import('file://' + path.join(CurrentPath, './napcat.mjs'));"
        "})();"
    )

    def _generate_launcher_files(self, root: Path) -> bool:
        """生成 launcher.sh 和 loadNapCat.js（仅在不存在时生成，不覆盖已有文件）。"""
        ok = True
        launcher = root / "launcher.sh"
        if not launcher.exists():
            try:
                launcher.write_text(self.LAUNCHER_SH_CONTENT, encoding="utf-8")
                launcher.chmod(0o755)
                self.log_lines.append(f"[download] ✔ 已生成 launcher.sh: {launcher}")
            except Exception as e:
                self.log_lines.append(f"[download] ✘ 生成 launcher.sh 失败: {e}")
                ok = False
        else:
            self.log_lines.append(f"[download] ℹ launcher.sh 已存在，跳过: {launcher}")

        loader = root / "loadNapCat.js"
        if not loader.exists():
            try:
                loader.write_text(self.LOAD_NAPCAT_JS_CONTENT, encoding="utf-8")
                self.log_lines.append(f"[download] ✔ 已生成 loadNapCat.js: {loader}")
            except Exception as e:
                self.log_lines.append(f"[download] ✘ 生成 loadNapCat.js 失败: {e}")
                ok = False
        return ok

    async def _download_and_compile_launcher_so(self, root: Path) -> bool:
        """下载 launcher.cpp 并编译为 libnapcat_launcher.so（g++ -shared -fPIC -ldl）。
        编译失败即视为源码被篡改或工具链缺失，直接报错。"""
        cpp_path = root / "launcher.cpp"
        so_path = root / "libnapcat_launcher.so"

        if so_path.exists():
            self.log_lines.append(f"[install] ✔ libnapcat_launcher.so 已存在，跳过编译: {so_path}")
            return True

        # 构建下载源：官方 raw + 用户镜像 + 内置镜像
        user_mirror = (self.plugin.config.get("napcat_download_mirror") or "").strip()
        prefixes: List[str] = []
        if user_mirror:
            prefixes.append(user_mirror)
        for m in NAPCAT_MIRROR_CANDIDATES:
            if m not in prefixes:
                prefixes.append(m)
        urls: List[Tuple[str, str]] = []
        for p in prefixes:
            urls.append((f"{p.rstrip('/')}/{NAPCAT_LINUX_LAUNCHER_CPP}", p))
        urls.append((NAPCAT_LINUX_LAUNCHER_CPP, "github-raw"))

        downloaded = False
        for idx, (url, label) in enumerate(urls, 1):
            self.log_lines.append(f"[install] [{idx}/{len(urls)}] 下载 launcher.cpp: {label}")
            try:
                ssl_ctx = make_ssl_context(strict=(label == "github-raw"))
                connector = aiohttp.TCPConnector(ssl=ssl_ctx)
                async with aiohttp.ClientSession(connector=connector) as session:
                    async with session.get(
                        url,
                        timeout=aiohttp.ClientTimeout(total=60, sock_connect=10, sock_read=30),
                    ) as resp:
                        if resp.status != 200:
                            self.log_lines.append(f"[install]   HTTP {resp.status}，跳过")
                            continue
                        text = await resp.text()
                if not text or "extern \"C\"" not in text:
                    self.log_lines.append("[install]   ✘ 内容不像是 launcher.cpp 源码，跳过")
                    continue
                cpp_path.write_text(text, encoding="utf-8")
                downloaded = True
                self.log_lines.append(f"[install]   ✔ launcher.cpp 下载成功 ({len(text)} 字节)")
                break
            except Exception as e:
                self.log_lines.append(f"[install]   下载异常: {e}")
                continue

        if not downloaded:
            self.log_lines.append("[install] ✘ launcher.cpp 所有来源均下载失败")
            return False

        # 编译：g++ -shared -fPIC launcher.cpp -o libnapcat_launcher.so -ldl
        if not shutil.which("g++"):
            self.log_lines.append("[install] ✘ 未找到 g++，无法编译 libnapcat_launcher.so")
            self.log_lines.append("[install]   请执行: sudo apt-get install -y g++")
            return False

        self.log_lines.append("[install] 正在编译 libnapcat_launcher.so ...")
        try:
            proc = await asyncio.create_subprocess_exec(
                "g++", "-shared", "-fPIC", str(cpp_path),
                "-o", str(so_path), "-ldl",
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
                cwd=str(root),
            )
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=120)
        except asyncio.TimeoutError:
            self.log_lines.append("[install] ✘ 编译超时")
            return False
        except Exception as e:
            self.log_lines.append(f"[install] ✘ 编译异常: {e}")
            return False

        if proc.returncode != 0 or not so_path.exists():
            self.log_lines.append(
                f"[install] ✘ 编译失败 (exit {proc.returncode}): "
                f"{out.decode('utf-8','ignore')[-300:] if out else ''}"
            )
            return False

        self.log_lines.append(f"[install] ✔ 编译成功: {so_path} ({so_path.stat().st_size} 字节)")
        return True

    async def _extract_napcat(self, archive_path: Path) -> bool:
        try:
            self.napcat_dir.mkdir(parents=True, exist_ok=True)
            with zipfile.ZipFile(archive_path, "r") as zf:
                zf.extractall(self.napcat_dir)

            if not IS_WINDOWS:
                try:
                    # 给所有 .sh 加可执行位
                    for sh in self.napcat_dir.rglob("*.sh"):
                        try:
                            st = sh.stat()
                            sh.chmod(st.st_mode | 0o555)
                        except Exception:
                            pass
                    # 生成 launcher.sh + loadNapCat.js（仅在不存在时，不覆盖）
                    self._generate_launcher_files(self.napcat_dir)
                    # napcat.mjs 保证可读
                    for name in ("napcat.mjs",):
                        p = self.napcat_dir / name
                        if p.exists():
                            try:
                                st = p.stat()
                                p.chmod(st.st_mode | 0o644)
                            except Exception:
                                pass
                except Exception as e:
                    self.log_lines.append(f"[download] ⚠ 后处理失败: {e}")

            if not self._find_entry():
                return False
            return True
        except Exception as e:
            logger.error(f"Extract failed: {e}")
            return False

    async def download_napcat(self, version: Optional[str] = None) -> bool:
        if getattr(self, "_downloading", False):
            return False
        self._downloading = True
        self._reset_download_state()
        self.download_state["downloading"] = True
        self.download_state["phase"] = "downloading"
        self.download_state["message"] = "正在下载 NapCat.Shell.zip"
        self.download_state["current_mirror"] = "准备下载"
        self.log_lines.append("[download] 开始下载 NapCat.Shell.zip ...")

        try:
            self.napcat_dir.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            self.log_lines.append(f"[download] ✘ 无法创建目录 {self.napcat_dir}: {e}")
            self._downloading = False
            self.download_state["downloading"] = False
            self.download_state["phase"] = "failed"
            return False

        try:
            if version and str(version).strip():
                version = str(version).strip()
            else:
                version = self.plugin.config.get("napcat_version", DEFAULT_NAPCAT_VERSION)
            if "/" in version:
                fixed = version.split("/", 1)[0].strip()
                if fixed:
                    version = fixed
                    self.plugin.config["napcat_version"] = fixed
                    try:
                        self.plugin._save_config(self.plugin.config)
                    except Exception:
                        pass
                else:
                    version = DEFAULT_NAPCAT_VERSION

            filename = "NapCat.Shell.zip"

            for local_zip in [
                self.napcat_dir / filename,
                self.napcat_dir / "napcat.shell.zip",
                self.plugin_dir / filename,
                self.plugin_dir / "napcat.shell.zip",
            ]:
                if local_zip.exists():
                    self.log_lines.append(f"[download] 发现本地压缩包: {local_zip}")
                    ok = await self._extract_napcat(local_zip)
                    if ok:
                        self.download_state["phase"] = "done"
                        self.download_state["percent"] = 100
                        self.log_lines.append("[download] ✔ 本地压缩包解压成功")
                    else:
                        self.download_state["phase"] = "failed"
                        self.log_lines.append("[download] ✘ 本地压缩包解压失败")
                    return ok

            # 自动解析版本（"latest" -> GitHub API），并获取官方 SHA256 基准
            self.log_lines.append(f"[download] 解析目标版本: {version}")
            rel = await self._resolve_napcat_release(version)
            tag = rel.get("tag") or version
            official_zip = rel.get("zip_url") or f"{NAPCAT_RELEASE_BASE}/{version}/{filename}"
            expected_sha = (rel.get("sha256") or "").lower() or None
            self.log_lines.append(f"[download] 解析到版本: {tag}")
            if expected_sha:
                self.log_lines.append(f"[download] 官方 SHA256: {expected_sha[:16]}...")
            else:
                self.log_lines.append("[download] ⚠ 该版本未发布 SHA256 清单，将仅做 zip 完整性校验")

            user_mirror = (self.plugin.config.get("napcat_download_mirror") or "").strip()
            prefixes: List[str] = []
            if user_mirror:
                prefixes.append(user_mirror)
            for m in NAPCAT_MIRROR_CANDIDATES:
                if m not in prefixes:
                    prefixes.append(m)

            urls: List[Tuple[str, str]] = []
            for p in prefixes:
                urls.append((f"{p.rstrip('/')}/{official_zip}", p))
            urls.append((official_zip, "official"))

            download_path = self.napcat_dir / filename
            self.download_state["total_mirrors"] = len(urls)

            for idx, (url, label) in enumerate(urls, 1):
                self.log_lines.append(f"[download] [{idx}/{len(urls)}] 尝试: {label}")
                self.download_state["current_mirror"] = label
                self.download_state["current_index"] = idx
                # 官方源一律严格 TLS；镜像源放开 TLS（但仍受 SHA256 校验兜底）
                ssl_ctx = make_ssl_context(strict=(label == "official"))
                connector = aiohttp.TCPConnector(ssl=ssl_ctx)
                session = aiohttp.ClientSession(connector=connector)
                try:
                    async with session:
                        async with session.get(
                            url,
                            timeout=aiohttp.ClientTimeout(total=None, sock_connect=10, sock_read=30),
                        ) as resp:
                            if resp.status != 200:
                                self.log_lines.append(
                                    f"[download] [{idx}/{len(urls)}] {label} HTTP {resp.status}，换下一个"
                                )
                                continue
                            total = resp.content_length or 0
                            downloaded = 0
                            start = time.time()
                            last_update = start
                            with open(download_path, "wb") as f:
                                async for chunk in resp.content.iter_chunked(1024 * 128):
                                    f.write(chunk)
                                    downloaded += len(chunk)
                                    now = time.time()
                                    if now - last_update >= 0.4:
                                        elapsed = now - start
                                        speed = downloaded / elapsed / 1024 if elapsed > 0 else 0
                                        self.download_state["downloaded_mb"] = downloaded / 1024 / 1024
                                        self.download_state["speed_kbps"] = speed
                                        if total > 0:
                                            self.download_state["total_mb"] = total / 1024 / 1024
                                            self.download_state["percent"] = downloaded * 100 // total
                                        last_update = now

                    # ---- SHA256 校验（防中间人投毒）----
                    if expected_sha:
                        actual_sha = self._sha256_file(download_path)
                        if actual_sha != expected_sha:
                            self.log_lines.append(
                                f"[download] [{idx}/{len(urls)}] ✘ SHA256 不匹配（来源 {label}），疑似被篡改，丢弃"
                            )
                            try:
                                download_path.unlink()
                            except Exception:
                                pass
                            continue
                        self.log_lines.append(f"[download] [{idx}/{len(urls)}] SHA256 校验通过")
                    else:
                        # 无官方哈希时，至少做 zip 结构完整性测试
                        try:
                            with zipfile.ZipFile(download_path) as zf:
                                bad = zf.testzip()
                            if bad is not None:
                                self.log_lines.append(f"[download] [{idx}/{len(urls)}] ✘ zip 损坏: {bad}")
                                try:
                                    download_path.unlink()
                                except Exception:
                                    pass
                                continue
                        except Exception as e:
                            self.log_lines.append(f"[download] [{idx}/{len(urls)}] ✘ 非有效 zip: {e}")
                            try:
                                download_path.unlink()
                            except Exception:
                                pass
                            continue

                    success = await self._extract_napcat(download_path)
                    if success:
                        try:
                            download_path.unlink()
                        except Exception:
                            pass
                        self.download_state["phase"] = "done"
                        self.download_state["percent"] = 100
                        self.log_lines.append(f"[download] ✔ 下载并解压成功（版本 {tag}，来源: {label}）")
                        return True
                    try:
                        if download_path.exists():
                            download_path.unlink()
                    except Exception:
                        pass
                    continue
                except Exception as e:
                    self.log_lines.append(f"[download] [{idx}/{len(urls)}] {label} 失败: {e}")
                    try:
                        if download_path.exists():
                            download_path.unlink()
                    except Exception:
                        pass
                    continue

            self.log_lines.append("[download] ✘ 所有镜像均失败")
            self.download_state["phase"] = "failed"
            return False
        finally:
            self._downloading = False
            self.download_state["downloading"] = False

    # ---------- LinuxQQ ----------

    def _build_linuxqq_candidate_urls(self) -> List[Tuple[str, str]]:
        """QQ deb 候选源：先套用户配置+内置加速镜像，最后回退 GitHub 直链。"""
        candidates: List[Tuple[str, str]] = []
        seen = set()

        def add(u: str, label: str):
            if u and u not in seen:
                seen.add(u)
                candidates.append((u, label))

        user_mirror = (self.plugin.config.get("napcat_download_mirror") or "").strip()
        mirror_order: List[str] = []
        if user_mirror:
            mirror_order.append(user_mirror)
        for m in NAPCAT_MIRROR_CANDIDATES:
            if m not in mirror_order:
                mirror_order.append(m)

        for raw in LINUXQQ_DEB_URLS:
            if "github.com/" in raw or "githubusercontent.com/" in raw:
                for m in mirror_order:
                    add(f"{m.rstrip('/')}/{raw}", m)
                add(raw, "nc-go-releases")
            else:
                add(raw, "qq-cdn")
        return candidates

    async def _purge_linuxqq(self) -> None:
        pw = (self.plugin.config.get("sudo_password", "") or "").strip()
        if not pw or not shutil.which("sudo"):
            return
        env = {**os.environ}
        await self._sudo_warm_async(pw)
        try:
            for args in (["dpkg", "-r", "linuxqq"], ["dpkg", "--purge", "linuxqq"]):
                self.log_lines.append(f"[qq-compat] 执行: sudo {' '.join(args)}")
                try:
                    proc = await asyncio.create_subprocess_exec(
                        "sudo", *args,
                        stdin=asyncio.subprocess.DEVNULL,
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.STDOUT,
                        env=env,
                    )
                    try:
                        out, _ = await asyncio.wait_for(proc.communicate(), timeout=180)
                        text = (out or b"").decode("utf-8", errors="ignore")
                        for ln in text.splitlines()[-5:]:
                            if ln.strip():
                                self.log_lines.append(f"[qq-compat] {ln.strip()}")
                    except asyncio.TimeoutError:
                        try:
                            proc.kill()
                        except Exception:
                            pass
                        self.log_lines.append("[qq-compat] ⚠ dpkg 卸载超时")
                except Exception as e:
                    self.log_lines.append(f"[qq-compat] ⚠ dpkg 卸载异常: {e}")
        finally:
            pass

    async def ensure_linux_qq_compatible(self) -> bool:
        cur_ver = get_linux_qq_version()
        cur_code = get_linux_qq_build_code()

        if is_qq_installed() and is_linux_qq_version_compatible():
            self.log_lines.append(
                f"[qq-compat] ✔ 当前 QQ {cur_ver} (build {cur_code}) "
                f"在支持范围 [{NAPCAT_SUPPORTED_QQ_MIN}, {NAPCAT_SUPPORTED_QQ_MAX}] 内，跳过"
            )
            return True

        if is_qq_installed():
            self.log_lines.append(
                f"[qq-compat] ⚠ 当前 QQ {cur_ver} (build {cur_code}) 不兼容："
                f"PacketBackend 要求 build ∈ [{NAPCAT_SUPPORTED_QQ_MIN}, {NAPCAT_SUPPORTED_QQ_MAX}]"
            )
            self.log_lines.append(
                f"[qq-compat] 将自动卸载并安装推荐版本 {RECOMMENDED_LINUXQQ_VERSION}"
            )
            await self._purge_linuxqq()
            if is_qq_installed():
                self.log_lines.append(
                    "[qq-compat] ⚠ purge 后仍检测到 QQ，可能卸载失败，继续尝试安装"
                )
        else:
            self.log_lines.append("[qq-compat] 未检测到 LinuxQQ，准备安装推荐版本 ...")

        for stale in list(self.napcat_dir.glob("linuxqq*.deb")) + \
                     list(self.napcat_dir.glob("QQ*.deb")):
            try:
                self.log_lines.append(f"[qq-compat] 移除旧 deb 缓存: {stale.name}")
                stale.unlink()
            except Exception:
                pass

        ok = await self.ensure_linux_qq()
        if not ok:
            return False

        new_ver = get_linux_qq_version()
        new_code = get_linux_qq_build_code()
        if is_linux_qq_version_compatible():
            self.log_lines.append(
                f"[qq-compat] ✔ 安装完成，当前 QQ {new_ver} (build {new_code}) 已兼容"
            )
            return True
        self.log_lines.append(
            f"[qq-compat] ✘ 安装后 QQ {new_ver} (build {new_code}) 仍不兼容，"
            f"请手动下载 build ∈ [{NAPCAT_SUPPORTED_QQ_MIN}, {NAPCAT_SUPPORTED_QQ_MAX}] "
            f"的 deb 放到: {self.napcat_dir}/"
        )
        return False

    async def ensure_linux_qq(self) -> bool:
        if is_qq_installed():
            self.log_lines.append("[qq-install] ✔ LinuxQQ 已安装，跳过")
            return True

        self.log_lines.append(
            f"[qq-install] 开始安装 LinuxQQ（目标版本 {RECOMMENDED_LINUXQQ_VERSION}）..."
        )

        pw = (self.plugin.config.get("sudo_password", "") or "").strip()
        if not pw:
            self.log_lines.append("[qq-install] ✘ 未配置 sudo 密码，无法安装 QQ")
            return False
        if not shutil.which("sudo"):
            self.log_lines.append("[qq-install] ✘ 系统无 sudo 命令")
            return False

        env_base = {
            **os.environ, "TERM": "dumb", "NO_COLOR": "1",
            "DEBIAN_FRONTEND": "noninteractive",
        }

        deb_path = self.napcat_dir / "linuxqq.deb"
        downloaded = False

        manual = list(self.napcat_dir.glob("linuxqq*.deb")) + \
                 list(self.napcat_dir.glob("QQ*.deb"))
        if manual:
            for m in manual:
                if not await self._validate_deb(m):
                    self.log_lines.append(f"[qq-install] ⚠ 本地 deb 无效: {m}，忽略")
                    continue
                ok_build, ver_str, build_code = is_deb_build_compatible(m)
                if ok_build:
                    deb_path = m
                    self.log_lines.append(
                        f"[qq-install] ✔ 使用本地 deb: {deb_path} "
                        f"(version={ver_str}, build={build_code})"
                    )
                    downloaded = True
                    break
                else:
                    self.log_lines.append(
                        f"[qq-install] ⚠ 本地 deb 的 build 不兼容，跳过: "
                        f"{m.name} (version={ver_str}, build={build_code})"
                    )

        if not downloaded:
            candidates = self._build_linuxqq_candidate_urls()
            total_urls = len(candidates)
            self.download_state["downloading"] = True
            self.download_state["phase"] = "downloading"
            self.download_state["message"] = "正在下载 LinuxQQ"
            self.download_state["current_mirror"] = "下载 LinuxQQ"
            self.download_state["total_mirrors"] = total_urls
            self.download_state["percent"] = 0

            for idx, (url, label) in enumerate(candidates, 1):
                short = url[:90] + ("..." if len(url) > 90 else "")
                self.log_lines.append(f"[qq-install] [{idx}/{total_urls}] 尝试 ({label}): {short}")
                self.download_state["current_index"] = idx
                self.download_state["current_mirror"] = f"LinuxQQ ({idx}/{total_urls}) - {label}"
                try:
                    if deb_path.exists():
                        try:
                            deb_path.unlink()
                        except Exception:
                            pass
                except Exception:
                    pass

                ok = await self._download_file(
                    url, deb_path, connect_timeout=15, read_timeout=60,
                    progress_label=f"LinuxQQ ({idx}/{total_urls})",
                    progress_index=idx, progress_total=total_urls, max_time=900,
                )
                if not ok:
                    continue
                if not deb_path.exists() or deb_path.stat().st_size < 1024:
                    self.log_lines.append(f"[qq-install] [{idx}/{total_urls}] ✘ 文件过小")
                    continue
                if not await self._validate_deb(deb_path):
                    self.log_lines.append(f"[qq-install] [{idx}/{total_urls}] ✘ 不是有效 deb，换下一个")
                    try:
                        deb_path.unlink()
                    except Exception:
                        pass
                    continue

                ok_build, ver_str, build_code = is_deb_build_compatible(deb_path)
                if not ok_build:
                    if ver_str is None:
                        self.log_lines.append(
                            f"[qq-install] [{idx}/{total_urls}] ✘ 无法读取 deb 版本，换下一个"
                        )
                    else:
                        self.log_lines.append(
                            f"[qq-install] [{idx}/{total_urls}] ✘ deb 内 build 不兼容，"
                            f"跳过: version={ver_str} build={build_code}"
                        )
                    try:
                        deb_path.unlink()
                    except Exception:
                        pass
                    continue

                size_mb = deb_path.stat().st_size / 1024 / 1024
                self.log_lines.append(
                    f"[qq-install] [{idx}/{total_urls}] ✔ 下载成功 "
                    f"({size_mb:.1f} MB, version={ver_str}, build={build_code})"
                )
                downloaded = True
                break

            self.download_state["downloading"] = False

        if not downloaded:
            self.log_lines.append(
                "[qq-install] ✘ 所有 LinuxQQ 下载源均失败或均不兼容。"
                "请手动下载 build 号 ∈ ["
                f"{NAPCAT_SUPPORTED_QQ_MIN}, {NAPCAT_SUPPORTED_QQ_MAX}] 的 deb 放到: "
                + str(self.napcat_dir)
            )
            self.log_lines.append("[qq-install] 手动下载地址（浏览器打开，任选其一）：")
            self.log_lines.append("[qq-install]   https://github.com/SATA-F5/NC_Go/releases")
            self.log_lines.append("[qq-install]   https://github.com/Rodert/qq-versions/releases")
            self.download_state["phase"] = "failed"
            self.download_state["message"] = "LinuxQQ 下载失败"
            return False

        self.download_state["phase"] = "extracting"
        self.download_state["message"] = "正在安装 LinuxQQ"
        self.download_state["current_mirror"] = "安装 LinuxQQ"
        self.download_state["percent"] = 0

        env = dict(env_base)
        if not await self._sudo_warm_async(pw):
            self.log_lines.append("[qq-install] ✘ sudo 密码验证失败")
            self.download_state["phase"] = "failed"
            return False

        try:
            self.log_lines.append("[qq-install] 执行: sudo apt-get update")
            proc = await asyncio.create_subprocess_exec(
                "sudo", "apt-get", "update",
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT, env=env,
            )
            try:
                await asyncio.wait_for(proc.communicate(), timeout=300)
            except asyncio.TimeoutError:
                try:
                    proc.kill()
                except Exception:
                    pass

            self.log_lines.append(f"[qq-install] 执行: sudo dpkg -i {deb_path.name}")
            proc = await asyncio.create_subprocess_exec(
                "sudo", "dpkg", "-i", str(deb_path),
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT, env=env,
            )
            try:
                out, _ = await asyncio.wait_for(proc.communicate(), timeout=300)
            except asyncio.TimeoutError:
                try:
                    proc.kill()
                except Exception:
                    pass
                self.log_lines.append("[qq-install] ✘ dpkg 安装超时")
                self.download_state["phase"] = "failed"
                self.download_state["message"] = "dpkg 超时"
                return False

            dpkg_rc = proc.returncode or 0
            text = (out or b"").decode("utf-8", errors="ignore")
            for ln in text.splitlines()[-8:]:
                if ln.strip():
                    self.log_lines.append(f"[qq-install] {ln.strip()}")

            if dpkg_rc != 0:
                self.log_lines.append("[qq-install] dpkg 返回非 0，执行 apt-get -f install -y 补依赖 ...")
                proc = await asyncio.create_subprocess_exec(
                    "sudo", "apt-get", "-f", "install", "-y",
                    stdin=asyncio.subprocess.DEVNULL,
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT, env=env,
                )
                try:
                    out, _ = await asyncio.wait_for(proc.communicate(), timeout=600)
                except asyncio.TimeoutError:
                    try:
                        proc.kill()
                    except Exception:
                        pass
                    self.log_lines.append("[qq-install] ✘ apt-get -f install 超时")
                    self.download_state["phase"] = "failed"
                    self.download_state["message"] = "apt-get 超时"
                    return False
                text = (out or b"").decode("utf-8", errors="ignore")
                for ln in text.splitlines()[-8:]:
                    if ln.strip():
                        self.log_lines.append(f"[qq-install] {ln.strip()}")

            if is_qq_installed():
                installed_ver = get_linux_qq_version()
                installed_code = get_linux_qq_build_code()
                self.log_lines.append(
                    f"[qq-install] ✔ LinuxQQ 安装成功: {installed_ver} (build {installed_code})"
                )
                try:
                    deb_path.unlink()
                except Exception:
                    pass
                self.download_state["phase"] = "done"
                self.download_state["downloading"] = False
                self.download_state["percent"] = 100
                self.download_state["current_mirror"] = ""
                self.download_state["message"] = "LinuxQQ 已安装"
                return True
            else:
                self.log_lines.append("[qq-install] ✘ 安装后仍未检测到 /opt/QQ/qq")
                self.download_state["phase"] = "failed"
                self.download_state["downloading"] = False
                self.download_state["message"] = "LinuxQQ 安装失败"
                return False
        finally:
            pass

    # ---------- Linux 本地安装（不再下载 install.sh） ----------

    async def _backup_inner_napcat_if_needed(self) -> bool:
        inner = self.napcat_dir / "napcat"
        if not inner.exists():
            return True
        try:
            has_content = any(inner.iterdir())
        except Exception:
            has_content = False
        if not has_content:
            return True

        if self._is_new_style_install(inner) or self._is_new_style_install(self.napcat_dir):
            self.log_lines.append(f"[install] {inner} 已包含 launcher，跳过备份")
            return True

        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        bak = self.napcat_dir / f"napcat.bak.{ts}"
        try:
            shutil.move(str(inner), str(bak))
            self.log_lines.append(
                f"[install] ⚠ 检测到 {inner} 已存在且不为空，已备份为 {bak.name}"
            )
            return True
        except Exception as e:
            self.log_lines.append(f"[install] ⚠ 备份 {inner} 失败: {e}")
            try:
                shutil.rmtree(inner)
                self.log_lines.append(f"[install] ✔ 已删除 {inner}")
                return True
            except Exception as e2:
                self.log_lines.append(f"[install] ✘ 删除 {inner} 也失败: {e2}")
                self.log_lines.append("[install] 请手动执行：")
                self.log_lines.append(f"[install]   rm -rf {inner}")
                self.log_lines.append("[install] 或带 sudo：")
                self.log_lines.append(f"[install]   sudo rm -rf {inner}")
                return False

    async def install_linux_napcat(self, version: Optional[str] = None) -> bool:
        """Linux native 一键安装（与 Ubuntu 实测方案一致）：
        装 QQ -> 装依赖(xvfb/g++) -> 下载 NapCat.Shell.zip(SHA256) -> 解压
        -> 下载 launcher.cpp 编译 libnapcat_launcher.so -> 生成 launcher.sh。
        不再 sudo bash install.sh，不再 patch QQ 磁盘文件（.so 运行时 LD_PRELOAD hook）。
        """
        self.napcat_dir.mkdir(parents=True, exist_ok=True)
        self._reset_download_state()
        self.download_state["downloading"] = True
        self.download_state["phase"] = "downloading"
        self.download_state["message"] = "正在准备 Linux 安装"
        self.download_state["current_mirror"] = "准备中"

        # 1) 装/校验系统 LinuxQQ（dpkg -i，需要 sudo；不兼容则自动 purge 后装推荐版）
        try:
            ok_qq = await self.ensure_linux_qq_compatible()
        except Exception as e:
            self.log_lines.append(f"[install] ✘ LinuxQQ 安装异常: {e}")
            self.download_state["phase"] = "failed"
            self.download_state["downloading"] = False
            self.download_state["message"] = f"LinuxQQ 安装失败: {e}"
            return False
        if not ok_qq:
            self.log_lines.append("[install] ✘ LinuxQQ 未就绪，中止 NapCat 安装")
            self.download_state["phase"] = "failed"
            self.download_state["downloading"] = False
            self.download_state["message"] = "LinuxQQ 未就绪"
            return False

        # 1.5) 确保编译/运行依赖：g++（编译 .so）+ xvfb（launcher.sh 用 Xvfb :1）
        missing_deps = []
        if not shutil.which("g++"):
            missing_deps.append("g++")
        if not shutil.which("Xvfb"):
            missing_deps.append("xvfb")
        if missing_deps:
            self.log_lines.append(f"[install] 缺少依赖: {', '.join(missing_deps)}，尝试安装 ...")
            pw = (self.plugin.config.get("sudo_password", "") or "").strip()
            if pw and await self._sudo_warm_async(pw):
                env = {**os.environ, "TERM": "dumb", "DEBIAN_FRONTEND": "noninteractive"}
                try:
                    proc = await asyncio.create_subprocess_exec(
                        "sudo", "-S", "apt-get", "install", "-y", *missing_deps,
                        stdin=asyncio.subprocess.PIPE,
                        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
                        env=env,
                    )
                    out, _ = await proc.communicate(input=(pw + "\n").encode(), timeout=300)
                    if proc.returncode == 0:
                        self.log_lines.append(f"[install] ✔ 依赖安装成功: {', '.join(missing_deps)}")
                    else:
                        self.log_lines.append(
                            f"[install] ✘ 依赖安装失败: "
                            f"{out.decode('utf-8','ignore')[-200:]}"
                        )
                        self.download_state["phase"] = "failed"
                        self.download_state["downloading"] = False
                        self.download_state["message"] = f"缺少依赖: {', '.join(missing_deps)}"
                        return False
                except Exception as e:
                    self.log_lines.append(f"[install] ✘ 依赖安装异常: {e}")
                    self.download_state["phase"] = "failed"
                    self.download_state["downloading"] = False
                    return False
            else:
                self.log_lines.append(f"[install] ✘ 无 sudo 密码，无法安装依赖: {', '.join(missing_deps)}")
                self.download_state["phase"] = "failed"
                self.download_state["downloading"] = False
                self.download_state["message"] = f"缺少依赖: {', '.join(missing_deps)}"
                return False

        # 2) 清理旧目录
        if not await self._backup_inner_napcat_if_needed():
            self.download_state["phase"] = "failed"
            self.download_state["downloading"] = False
            self.download_state["message"] = "无法清理旧的 napcat 目录"
            return False

        # 3) 下载 + SHA256 校验 + 解压 NapCat.Shell.zip
        self.log_lines.append("[install] 开始下载 NapCat.Shell.zip（自动解析最新版，含 SHA256 校验）...")
        try:
            ok_zip = await self.download_napcat(version=version)
        except Exception as e:
            self.log_lines.append(f"[install] ✘ NapCat 下载异常: {e}")
            self.download_state["phase"] = "failed"
            self.download_state["downloading"] = False
            self.download_state["message"] = f"NapCat 下载失败: {e}"
            return False
        if not ok_zip:
            self.log_lines.append("[install] ✘ NapCat.Shell.zip 下载/校验失败")
            self.download_state["phase"] = "failed"
            self.download_state["downloading"] = False
            self.download_state["message"] = "NapCat 下载或 SHA256 校验失败"
            return False

        # 4) 下载 launcher.cpp 并编译 libnapcat_launcher.so
        self.log_lines.append("[install] 正在编译 libnapcat_launcher.so（LD_PRELOAD 注入器）...")
        try:
            ok_so = await self._download_and_compile_launcher_so(self.napcat_dir)
        except Exception as e:
            self.log_lines.append(f"[install] ✘ launcher.so 编译异常: {e}")
            ok_so = False
        if not ok_so:
            self.download_state["phase"] = "failed"
            self.download_state["downloading"] = False
            self.download_state["message"] = "libnapcat_launcher.so 编译失败"
            return False

        # 5) 校验结构：launcher.sh + libnapcat_launcher.so
        ok = self._is_new_style_install(self.napcat_dir)
        if ok:
            self.log_lines.append(
                "[install] ✔ 安装完成（launcher.sh + libnapcat_launcher.so + napcat.mjs 已就位）"
            )
            self.download_state["phase"] = "done"
            self.download_state["downloading"] = False
            self.download_state["percent"] = 100
            self.download_state["current_mirror"] = ""
            self.download_state["message"] = "安装完成"
        else:
            self.log_lines.append("[install] ✘ 安装后未找到 launcher.sh / libnapcat_launcher.so")
            self.download_state["phase"] = "failed"
            self.download_state["downloading"] = False
            self.download_state["message"] = "安装后未找到启动器"
        return ok
