import asyncio
import base64
import json
import os
import platform
import shutil
import webbrowser
from pathlib import Path
from typing import Any, Dict, List, Optional

from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star
from astrbot.api import logger
from astrbot.api.web import json_response, error_response, request

try:
    from cryptography.hazmat.primitives.asymmetric import padding as asym_padding
    from cryptography.hazmat.primitives.asymmetric import rsa as asym_rsa
except ImportError:
    asym_padding = None
    asym_rsa = None

try:
    from astrbot.api import AstrBotConfig
except ImportError:
    AstrBotConfig = dict

from .utils import (
    PLUGIN_NAME, PLUGIN_VERSION, CONFIG_VERSION, DEFAULT_CONFIG,
    IS_WINDOWS, IS_LINUX,
    _mask_token,
    _encrypt_secret, _decrypt_secret,
    _detect_distro_family, detect_docker_binary,
    resolve_deploy_mode, resolve_persistent_dir,
    get_linux_qq_version, get_linux_qq_build_code,
    is_linux_qq_version_compatible, is_qq_installed,
    NAPCAT_SUPPORTED_QQ_MIN, NAPCAT_SUPPORTED_QQ_MAX,
    RECOMMENDED_LINUXQQ_VERSION,
    NAPCAT_MIRROR_CANDIDATES,
)
from .napcat_manager import NapCatManager
from .config_syncer import ConfigSyncer
from . import linux_tools

PLUGIN_CODE_VERSION = "2026-11-01-v750"


class NapCatGoPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig = None):
        super().__init__(context)
        self.astrbot_config = config

        logger.info(f"[NapCat Go] ============ CODE VERSION: {PLUGIN_CODE_VERSION} ============")

        self.persistent_dir = resolve_persistent_dir(context)
        logger.info(f"[NapCat Go] persistent dir: {self.persistent_dir}")

        self.config = self._load_config()
        self.syncer = ConfigSyncer(self)

        # One-shot sudo: in-memory RSA key only, never written to disk.
        # A restart invalidates the key and the frontend simply fetches a new one.
        self._sudo_key = (
            asym_rsa.generate_private_key(public_exponent=65537, key_size=2048)
            if asym_rsa is not None
            else None
        )

        self.deploy_mode = resolve_deploy_mode(
            self.config.get("deploy_mode", "auto"),
            docker_binary_available=detect_docker_binary() is not None,
        )
        logger.info(f"[NapCat Go] resolved deploy_mode = {self.deploy_mode}")
        self.manager = NapCatManager(self, deploy_mode=self.deploy_mode)
        logger.info(f"[NapCat Go] NapCat dir: {self.manager.napcat_dir}")

        def _reg(route, handler, methods, desc=""):
            try:
                context.register_web_api(route, handler, methods, desc)
            except TypeError:
                try:
                    context.register_web_api(route, handler, methods)
                except TypeError as e:
                    logger.error(f"register_web_api failed route={route}: {e}")

        _reg(f"/{PLUGIN_NAME}/status", self.api_status, ["GET"], "Get status")
        _reg(f"/{PLUGIN_NAME}/sudo/key", self.api_sudo_key, ["GET"], "Get sudo public key")
        _reg(f"/{PLUGIN_NAME}/sync", self.api_sync, ["POST"], "Sync now")
        _reg(f"/{PLUGIN_NAME}/refresh", self.api_refresh, ["POST"], "Refresh")
        _reg(f"/{PLUGIN_NAME}/scan", self.api_scan, ["POST"], "Scan dirs")
        _reg(f"/{PLUGIN_NAME}/config", self.api_get_config, ["GET"], "Get config")
        _reg(f"/{PLUGIN_NAME}/config/save", self.api_save_config, ["POST"], "Save config")
        _reg(f"/{PLUGIN_NAME}/start", self.api_start, ["POST"], "Start NapCat")
        _reg(f"/{PLUGIN_NAME}/stop", self.api_stop, ["POST"], "Stop NapCat")
        _reg(f"/{PLUGIN_NAME}/restart", self.api_restart, ["POST"], "Restart NapCat")
        _reg(f"/{PLUGIN_NAME}/install", self.api_install, ["POST"], "Install (Linux)")
        _reg(f"/{PLUGIN_NAME}/uninstall", self.api_uninstall, ["POST"], "Uninstall NapCat")
        _reg(f"/{PLUGIN_NAME}/releases", self.api_releases, ["GET"], "List NapCat releases")
        _reg(f"/{PLUGIN_NAME}/logs", self.api_get_logs, ["GET"], "Get logs")
        _reg(f"/{PLUGIN_NAME}/open-webui", self.api_open_webui, ["POST"], "Open WebUI")
        _reg(f"/{PLUGIN_NAME}/cleanup", self.api_cleanup, ["POST"], "Cleanup")
        _reg(f"/{PLUGIN_NAME}/onboarding/done", self.api_onboarding_done, ["POST"], "Mark onboarding done")
        _reg(f"/{PLUGIN_NAME}/ui/theme", self.api_save_theme, ["POST"], "Save UI theme")
        _reg(f"/{PLUGIN_NAME}/download/options", self.api_download_options, ["POST"], "Save download options")
        _reg(f"/{PLUGIN_NAME}/deps/status", self.api_deps_status, ["GET"], "Check Linux dependencies")
        _reg(f"/{PLUGIN_NAME}/linux/install-deps", self.api_install_linux_deps, ["POST"], "Install Linux dependencies")

        safe_config = {**self.config, "sudo_password": "***" if self.config.get("sudo_password") else ""}
        logger.info(f"[NapCat Go] FINAL CONFIG: {safe_config}")

        auto_start = bool(self.config.get("auto_start", True))
        installed = self.manager.is_installed()
        logger.info(f"[NapCat Go] auto_start={auto_start} installed={installed}")

        if auto_start:
            self._auto_start_task = asyncio.ensure_future(self._auto_start_wrapper())

    @filter.command("napcat")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def napcat_cmd(self, event: AstrMessageEvent):
        try:
            yield event.plain_result(self._status_text())
        except Exception as e:
            logger.error(f"[NapCat Go] napcat_cmd failed: {e}", exc_info=True)

    async def _auto_start_wrapper(self):
        try:
            await asyncio.sleep(3)
            ok = await self.manager.start(self._saved_sudo_password())
            if ok and self.manager.is_running():
                logger.info("[NapCat Go] auto_start: NapCat is running")
            else:
                logger.warning(f"[NapCat Go] auto_start: failed (returned {ok})")
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error(f"[NapCat Go] auto_start failed: {e}", exc_info=True)

    async def _start_after_install(self, sudo_password: str = ""):
        """Start once a fresh install has fully released its files.

        The password from the install request is reused only for this
        immediate post-install start; it is never written to disk.
        """
        try:
            await asyncio.sleep(1.0)
            if self.manager.is_running():
                return
            logger.info("[NapCat Go] install finished, starting NapCat automatically")
            ok = await self.manager.start(sudo_password)
            if ok and self.manager.is_running():
                logger.info("[NapCat Go] post-install auto_start: NapCat is running")
            else:
                logger.warning("[NapCat Go] post-install auto_start failed")
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error(f"[NapCat Go] post-install auto_start failed: {e}", exc_info=True)

    # ---------- API ----------

    async def api_status(self):
        try:
            self.syncer.refresh()
        except Exception:
            pass
        d = self.syncer.status_dict()
        d["running"] = self.manager.is_running()
        d["qq_processes"] = self._qq_process_list(d["running"])
        d["napcat_installed"] = self.manager.is_installed()
        d["qq_installed"] = True if self.deploy_mode == "docker" else is_qq_installed()
        d["deploy_mode"] = self.manager.deploy_mode
        d["distro_family"] = _detect_distro_family() if IS_LINUX else None
        d["docker_available"] = detect_docker_binary() is not None
        d["docker_container"] = self.manager.docker_container_name()
        d["docker_image"] = self.manager.docker_image_ref()
        d["napcat_webui_url"] = self.manager.napcat_webui_url
        d["napcat_token"] = _mask_token(self.manager.napcat_token)
        d["sudo_password_saved"] = bool(self._saved_sudo_password())
        d["napcat_port"] = self.manager.napcat_port
        d["napcat_qq_number"] = self.manager.napcat_qq_number
        d["download_state"] = dict(self.manager.download_state)
        d["log_count"] = len(self.manager.log_lines)
        d["plugin"] = {"version": PLUGIN_VERSION, "code_version": PLUGIN_CODE_VERSION}
        if IS_LINUX:
            d["linux_deps_missing"] = linux_tools.missing_linux_deps()

        if IS_LINUX:
            d["qq_version"] = get_linux_qq_version()
            d["qq_build"] = get_linux_qq_build_code()
            d["qq_version_compatible"] = is_linux_qq_version_compatible()
            d["qq_supported_build_min"] = NAPCAT_SUPPORTED_QQ_MIN
            d["qq_supported_build_max"] = NAPCAT_SUPPORTED_QQ_MAX
            d["qq_recommended_version"] = RECOMMENDED_LINUXQQ_VERSION
        else:
            d["qq_version"] = None
            d["qq_build"] = None
            d["qq_version_compatible"] = None

        try:
            root = self.manager._find_new_style_root()
            d["new_style_root"] = str(root) if root else None
            d["config_dir"] = str(self.manager._get_config_dir())
        except Exception:
            d["new_style_root"] = None
            d["config_dir"] = None

        qq_logged_in = self.manager._qq_logged_in
        qq_user_id = None
        qq_nickname = ""
        if d["running"] and self.manager.api:
            try:
                info = await self.manager.api.get_login_info()
                if info and isinstance(info, dict):
                    data = info.get("data") or {}
                    user_id = data.get("user_id")
                    if user_id:
                        qq_logged_in = True
                        qq_user_id = user_id
                        qq_nickname = data.get("nickname", "")
                        self.manager._qq_logged_in = True
                        if not self.manager.napcat_qq_number:
                            self.manager.napcat_qq_number = str(user_id)
                    else:
                        qq_logged_in = False
            except Exception:
                pass
        d["qq_logged_in"] = qq_logged_in
        d["qq_user_id"] = qq_user_id
        d["qq_nickname"] = qq_nickname
        return json_response(d)

    def _qq_process_list(self, running: bool) -> List[Dict[str, Any]]:
        """Lightweight process list for the QQ / NapCat section.

        NapCat itself owns the launcher process; on Docker we show the
        container instead. We deliberately avoid spawning tasklist/pgrep on
        every status poll.
        """
        if not running:
            return []
        if self.deploy_mode == "docker":
            return [{"pid": 0, "name": "Docker · " + self.manager.docker_container_name(), "online": True}]
        pids = sorted(getattr(self.manager, "_launched_pids", set()))
        if not pids:
            proc = getattr(self.manager, "process", None)
            if proc is not None and getattr(proc, "returncode", None) is None:
                pids = [proc.pid]
        return [{"pid": int(pid), "name": "NapCat / QQ", "online": True} for pid in pids]

    async def api_sudo_key(self):
        if self._sudo_key is None:
            return error_response("缺少 cryptography 依赖，无法加密 sudo 密码")
        numbers = self._sudo_key.public_key().public_numbers()
        n_hex = format(numbers.n, "x")
        if len(n_hex) % 2:
            n_hex = "0" + n_hex
        return json_response(
            {
                "ok": True,
                "n": n_hex,
                "e": format(numbers.e, "x"),
                "size": (numbers.n.bit_length() + 7) // 8,
            }
        )

    def _sudo_from_payload(self, payload: Any) -> tuple:
        """Decrypt sudo_password_enc from a request body.

        Returns (password, error). An empty password means the caller did not
        provide one. The plaintext password is only held in local variables.
        """
        if not isinstance(payload, dict):
            return "", ""
        encoded = str(payload.get("sudo_password_enc") or "").strip()
        if not encoded:
            return "", ""
        if self._sudo_key is None or asym_padding is None:
            return "", "缺少 cryptography 依赖，无法解密 sudo 密码"
        try:
            raw = base64.b64decode(encoded, validate=True)
            plain = self._sudo_key.decrypt(raw, asym_padding.PKCS1v15())
            return plain.decode("utf-8").strip(), ""
        except Exception:
            return "", "sudo 密码解密失败，请重试"

    async def _read_sudo_payload(self) -> tuple:
        try:
            payload = await request.json(default={})
        except Exception:
            payload = {}
        if not isinstance(payload, dict):
            payload = {}
        pw, err = self._sudo_from_payload(payload)
        if not pw:
            pw = self._saved_sudo_password()
        return payload, pw, err

    def _saved_sudo_password(self) -> str:
        return str(self.config.get("sudo_password", "") or "").strip()

    def _sudo_required(self, pw: str) -> bool:
        """Whether this native Linux action still needs a password."""
        if IS_WINDOWS or self.manager.deploy_mode == "docker":
            return False
        if not shutil.which("sudo"):
            return False
        try:
            if hasattr(os, "geteuid") and os.geteuid() == 0:
                return False
        except Exception:
            pass
        return not bool(pw)

    async def api_sync(self):
        try:
            _payload, pw, err = await self._read_sudo_payload()
            if err:
                return error_response(err)
            ok, msg = self.syncer.sync(sudo_password=pw)
            return json_response({"ok": ok, "message": msg, "status": self.syncer.status_dict()})
        except Exception as e:
            return error_response(f"Sync exception: {e}")

    async def api_refresh(self):
        try:
            self.syncer.refresh()
            return json_response({"ok": True, "status": self.syncer.status_dict()})
        except Exception as e:
            return error_response(f"Refresh failed: {e}")

    async def api_scan(self):
        try:
            return json_response({"ok": True, "candidates": self.syncer.scan_candidates()})
        except Exception as e:
            return error_response(f"Scan failed: {e}")

    async def api_get_config(self):
        data = {k: v for k, v in self.config.items() if k != "sudo_password"}
        data["sudo_password_saved"] = bool(self._saved_sudo_password())
        data["download_mirrors"] = list(NAPCAT_MIRROR_CANDIDATES)
        data["plugin_version"] = PLUGIN_VERSION
        return json_response(data)

    async def api_save_config(self):
        payload = await request.json(default={})
        if not isinstance(payload, dict):
            return error_response("Request body must be JSON")
        pw, sudo_err = self._sudo_from_payload(payload)
        if sudo_err:
            return error_response(sudo_err)
        new_pw = str(payload.get("sudo_password", "") or "").strip()
        if not new_pw and pw and payload.get("sudo_password_enc"):
            # The page encrypts the sudo password with the one-shot RSA key;
            # when it arrives through config/save it should be persisted too.
            new_pw = pw
        if new_pw:
            self.config["sudo_password"] = new_pw
            pw = new_pw
        elif not pw:
            pw = self._saved_sudo_password()
        deploy_mode = str(payload.get("deploy_mode", "") or "").strip().lower() or self.config.get("deploy_mode", "auto")
        if deploy_mode not in {"auto", "windows", "docker", "native"}:
            deploy_mode = "auto"
        self.config.update({
            "napcat_config_dir": str(payload.get("napcat_config_dir", "") or "").strip(),
            "astrbot_config_path": str(payload.get("astrbot_config_path", "") or "").strip(),
            "qq_number": str(payload.get("qq_number", "") or "").strip(),
            "deploy_mode": deploy_mode,
            "docker_image": str(payload.get("docker_image", self.config.get("docker_image", "mlikiowa/napcat-docker")) or "").strip(),
            "docker_tag": str(payload.get("docker_tag", self.config.get("docker_tag", "latest")) or "").strip(),
            "docker_container_name": str(payload.get("docker_container_name", self.config.get("docker_container_name", "napcat-go")) or "").strip(),
            "napcat_download_mirror": str(payload.get("napcat_download_mirror", self.config.get("napcat_download_mirror", "https://gh.zwy.one/"))),
            "napcat_version": str(payload.get("napcat_version", self.config.get("napcat_version", "latest")) or "latest").strip(),
        })
        try:
            port = int(payload.get("napcat_port", self.config.get("napcat_port", 6099)) or 6099)
        except (TypeError, ValueError):
            port = 6099
        if 1 <= port <= 65535:
            self.config["napcat_port"] = port
            self.manager.napcat_port = port
            if getattr(self.manager, "api", None) is not None:
                self.manager.api.update_endpoint(port=port)
        if "auto_start" in payload:
            self.config["auto_start"] = bool(payload.get("auto_start"))
        if "auto_sync" in payload:
            self.config["auto_sync"] = bool(payload.get("auto_sync"))
        if "auto_start_after_install" in payload:
            self.config["auto_start_after_install"] = bool(payload.get("auto_start_after_install"))
        self.deploy_mode = resolve_deploy_mode(
            self.config.get("deploy_mode", "auto"),
            docker_binary_available=detect_docker_binary() is not None,
        )
        self.manager.deploy_mode = self.deploy_mode
        self._save_config(self.config)
        try:
            ok, msg = self.syncer.sync(sudo_password=pw)
            if (not ok and not pw and self._sudo_required("")
                    and any(word in str(msg).lower() for word in
                            ("permission", "eacces", "denied", "read-only", "只读", "拒绝", "权限"))):
                return json_response({"ok": True, "sync_ok": False, "need_sudo": True,
                                      "sync_msg": msg, "status": self.syncer.status_dict()})
            return json_response({"ok": True, "sync_ok": ok, "sync_msg": msg,
                                  "status": self.syncer.status_dict()})
        except Exception as e:
            return json_response({"ok": True, "sync_ok": False,
                                  "sync_msg": f"Sync exception: {e}",
                                  "status": self.syncer.status_dict()})

    async def api_start(self):
        _payload, pw, err = await self._read_sudo_payload()
        if err:
            return error_response(err)
        if not self.manager.is_installed():
            return json_response({
                "ok": False,
                "need_install": True,
                "message": "未检测到已安装的 NapCat，请先下载并安装",
            })
        if self._sudo_required(pw):
            return json_response({"ok": False, "need_sudo": True, "message": "启动 NapCat 需要 sudo 密码"})
        ok = await self.manager.start(pw)
        return json_response({"ok": ok})

    async def api_stop(self):
        _payload, pw, err = await self._read_sudo_payload()
        if err:
            return error_response(err)
        if self._sudo_required(pw):
            return json_response({"ok": False, "need_sudo": True, "message": "停止 NapCat 需要 sudo 密码"})
        await self.manager.stop(pw)
        return json_response({"ok": True})

    async def api_restart(self):
        _payload, pw, err = await self._read_sudo_payload()
        if err:
            return error_response(err)
        if self._sudo_required(pw):
            return json_response({"ok": False, "need_sudo": True, "message": "重启 NapCat 需要 sudo 密码"})
        await self.manager.restart(pw)
        return json_response({"ok": True})

    async def api_install(self):
        if self.manager.process and self.manager.process.returncode is None:
            return error_response("NapCat 正在运行，请先停止后再安装新版本")
        payload, pw, err = await self._read_sudo_payload()
        if err:
            return error_response(err)
        version = str(payload.get("version", "") or "").strip() or None

        if self.manager.deploy_mode == "docker":
            try:
                ok = await self.manager._start_docker()
                return json_response({
                    "ok": ok,
                    "message": "Docker 容器已启动" if ok else "Docker 容器启动失败",
                })
            except Exception as e:
                return error_response(f"Docker install failed: {e}")
        if IS_WINDOWS:
            try:
                ok = await self.manager.download_napcat(version=version)
            except Exception as e:
                return error_response(f"Download failed: {e}")
        else:
            if self._sudo_required(pw):
                return json_response({"ok": False, "need_sudo": True, "message": "安装 NapCat 需要 sudo 密码"})
            try:
                ok = await self.manager.install_linux_napcat(version=version, sudo_password=pw)
            except Exception as e:
                return error_response(f"Install failed: {e}")

        auto_starting = False
        if ok and bool(self.config.get("auto_start_after_install", True)):
            self._post_install_task = asyncio.ensure_future(self._start_after_install(pw))
            auto_starting = True
        if ok:
            message = "安装完成，正在自动启动 NapCat" if auto_starting else "安装完成"
        else:
            message = "安装失败，请查看右侧日志"
        return json_response({"ok": ok, "auto_starting": auto_starting, "message": message})

    async def api_releases(self):
        try:
            releases = await self.manager.list_napcat_releases()
            return json_response({"ok": True, "releases": releases})
        except Exception as e:
            return error_response(f"List releases failed: {e}")

    async def api_uninstall(self):
        payload, pw, err = await self._read_sudo_payload()
        if err:
            return error_response(err)
        purge_qq = bool(payload.get("purge_qq", False))
        if self._sudo_required(pw):
            return json_response({"ok": False, "need_sudo": True, "message": "卸载 NapCat 需要 sudo 密码"})
        try:
            ok = await self.manager.uninstall_napcat(purge_qq=purge_qq, sudo_password=pw)
            return json_response({"ok": ok})
        except Exception as e:
            return error_response(f"Uninstall failed: {e}")

    async def api_get_logs(self):
        since = request.query.get("since", 0, type=int)
        lines = list(self.manager.log_lines)
        return json_response({"lines": lines[since:], "total": len(lines)})

    async def api_open_webui(self):
        url = self.manager.napcat_webui_url
        if not url:
            return error_response("WebUI URL not available")
        try:
            webbrowser.open(url)
            return json_response({"ok": True, "url": url})
        except Exception as e:
            return error_response(f"Open browser failed: {e}")

    async def api_cleanup(self):
        try:
            if self.manager.deploy_mode == "docker":
                name = self.manager.docker_container_name()
                code, out = await self.manager._docker_cmd("rm", "-f", name, timeout=60)
                if self.manager._log_task and not self.manager._log_task.done():
                    self.manager._log_task.cancel()
                    try:
                        await self.manager._log_task
                    except (asyncio.CancelledError, Exception):
                        pass
                    self.manager._log_task = None
                self.manager.process = None
                self.manager.napcat_webui_url = ""
                self.manager.napcat_token = ""
                self.manager._qq_logged_in = False
                return json_response({"ok": code == 0, "message": "容器已移除"})
            _payload, pw, err = await self._read_sudo_payload()
            if err:
                return error_response(err)
            await self.manager.stop(pw)
            self.manager.cleanup()
            return json_response({"ok": True, "message": "Cleaned"})
        except Exception as e:
            return error_response(f"Cleanup failed: {e}")

    async def api_download_options(self):
        try:
            payload = await request.json(default={})
        except Exception:
            payload = {}
        if not isinstance(payload, dict):
            payload = {}
        if "napcat_download_mirror" in payload:
            self.config["napcat_download_mirror"] = str(payload.get("napcat_download_mirror") or "").strip()
        if "napcat_version" in payload:
            self.config["napcat_version"] = str(payload.get("napcat_version") or "latest").strip() or "latest"
        self._save_config(self.config)
        return json_response({
            "ok": True,
            "napcat_download_mirror": self.config.get("napcat_download_mirror", ""),
            "napcat_version": self.config.get("napcat_version", "latest"),
        })

    async def api_deps_status(self):
        if not IS_LINUX:
            return json_response({"ok": True, "linux": False, "missing": []})
        return json_response({"ok": True, "linux": True, "missing": linux_tools.missing_linux_deps()})

    async def api_install_linux_deps(self):
        _payload, pw, err = await self._read_sudo_payload()
        if err:
            return error_response(err)
        if not pw:
            return json_response({"ok": False, "need_sudo": True, "message": "安装 Linux 依赖需要 sudo 密码"})
        result = await linux_tools.install_linux_deps(self, pw)
        return json_response(result)

    async def api_onboarding_done(self):
        self.config["onboarding_done"] = True
        self.config["onboarding_version"] = 3
        self._save_config(self.config)
        return json_response({"ok": True, "version": 3})

    async def api_save_theme(self):
        try:
            payload = await request.json(default={})
        except Exception:
            payload = {}
        if not isinstance(payload, dict):
            payload = {}
        theme = str(payload.get("theme") or "auto").strip().lower()
        if theme not in ("auto", "light", "dark"):
            theme = "auto"
        self.config["ui_theme"] = theme
        self._save_config(self.config)
        return json_response({"ok": True, "theme": theme})

    # ---------- config ----------

    def _load_config(self) -> Dict[str, Any]:
        self.config_path = self.persistent_dir / "config.json"
        config = DEFAULT_CONFIG.copy()

        persisted_user: Dict[str, Any] = {}
        if self.config_path.exists():
            try:
                with open(self.config_path, "r", encoding="utf-8-sig") as f:
                    persisted_user = json.load(f)
            except Exception:
                persisted_user = {}

        persisted_sudo_pw = ""
        if persisted_user.get("sudo_password"):
            persisted_sudo_pw = _decrypt_secret(str(persisted_user["sudo_password"]))

        for k in DEFAULT_CONFIG:
            if k == "sudo_password":
                continue
            if k in persisted_user:
                config[k] = persisted_user[k]

        obj_cfg: Dict[str, Any] = {}
        if self.astrbot_config is not None:
            try:
                if hasattr(self.astrbot_config, "keys"):
                    for k in self.astrbot_config.keys():
                        try:
                            obj_cfg[k] = self.astrbot_config[k]
                        except Exception:
                            pass
            except Exception:
                pass

        panel_cfg: Dict[str, Any] = {}
        panel_path = self._find_panel_config_path()
        if panel_path:
            try:
                with open(panel_path, "r", encoding="utf-8-sig") as f:
                    panel_cfg = json.load(f)
                if not isinstance(panel_cfg, dict):
                    panel_cfg = {}
            except Exception:
                panel_cfg = {}

        merged = dict(config)
        for k, v in obj_cfg.items():
            if k == "sudo_password":
                continue
            merged[k] = v
        for k, v in panel_cfg.items():
            if k == "sudo_password":
                continue
            if k in DEFAULT_CONFIG and v is not None:
                merged[k] = v

        final = DEFAULT_CONFIG.copy()
        for k in DEFAULT_CONFIG:
            if k == "sudo_password":
                continue
            if k in merged and merged[k] is not None:
                v = merged[k]
                if k in ("auto_sync", "auto_start", "auto_start_after_install", "onboarding_done"):
                    final[k] = v if isinstance(v, bool) else str(v).strip().lower() in ("1", "true", "yes", "on")
                elif k in ("napcat_port", "onboarding_version"):
                    try:
                        final[k] = int(v)
                    except (TypeError, ValueError):
                        pass
                else:
                    final[k] = str(v or "").strip()

        # Restore the saved sudo password (encrypted in config.json). Panel
        # config is also accepted for users coming from 7.4.1.
        if persisted_sudo_pw:
            final["sudo_password"] = persisted_sudo_pw
        else:
            panel_sudo_pw = ""
            for source in (panel_cfg, obj_cfg):
                v = source.get("sudo_password")
                if v and str(v).strip():
                    panel_sudo_pw = str(v).strip()
                    break
            if panel_sudo_pw:
                final["sudo_password"] = panel_sudo_pw

        if final.get("ui_theme") not in ("auto", "light", "dark"):
            final["ui_theme"] = "auto"

        to_save = dict(final)
        to_save["__config_version__"] = CONFIG_VERSION
        if to_save.get("sudo_password"):
            to_save["sudo_password"] = _encrypt_secret(to_save["sudo_password"])
        try:
            self.config_path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.config_path, "w", encoding="utf-8") as f:
                json.dump(to_save, f, indent=2, ensure_ascii=False)
        except Exception:
            pass
        return final

    def _find_panel_config_path(self) -> Optional[Path]:
        candidates: List[Path] = []
        try:
            data_root = self.plugin_dir.parent.parent
            candidates.append(data_root / "config" / f"{PLUGIN_NAME}_config.json")
            candidates.append(data_root / f"{PLUGIN_NAME}_config.json")
        except Exception:
            pass
        if hasattr(self.context, "get_data_dir"):
            try:
                gd = Path(self.context.get_data_dir())
                candidates.append(gd / "config" / f"{PLUGIN_NAME}_config.json")
                candidates.append(gd / f"{PLUGIN_NAME}_config.json")
            except Exception:
                pass
        seen = set()
        for c in candidates:
            try:
                ap = c.resolve()
            except Exception:
                continue
            if ap in seen:
                continue
            seen.add(ap)
            if ap.exists() and ap.is_file():
                return ap
        return None

    def _save_config(self, config: Dict[str, Any] = None):
        if config is None:
            config = self.config
        try:
            to_save = dict(config)
            to_save["__config_version__"] = CONFIG_VERSION
            if to_save.get("sudo_password"):
                to_save["sudo_password"] = _encrypt_secret(to_save["sudo_password"])
            self.config_path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.config_path, "w", encoding="utf-8") as f:
                json.dump(to_save, f, indent=2, ensure_ascii=False)
        except Exception as e:
            logger.error(f"Save config failed: {e}")

    def _status_text(self) -> str:
        s = self.syncer.status_dict()
        lines = ["NapCat Go Status", "-" * 20, f"Platform: {s['platform']}"]
        lines.append(f"deploy_mode: {self.deploy_mode}")
        if IS_LINUX:
            lines.append(f"distro: {_detect_distro_family()}")
            lines.append(f"QQ version: {get_linux_qq_version() or '(未安装)'}")
            lines.append(f"QQ build: {get_linux_qq_build_code()}")
            lines.append(
                f"QQ compatible: {'yes' if is_linux_qq_version_compatible() else 'NO'} "
                f"(需要 build ∈ [{NAPCAT_SUPPORTED_QQ_MIN}, {NAPCAT_SUPPORTED_QQ_MAX}])"
            )
            try:
                root = self.manager._find_new_style_root()
                lines.append(f"new_style_root: {root or '(未找到)'}")
                lines.append(f"config_dir: {self.manager._get_config_dir()}")
            except Exception:
                pass
        lines.append(f"QQ number (cached): {self.manager.napcat_qq_number or '(未识别)'}")
        lines.append(f"NapCat: {'running' if self.manager.is_running() else 'stopped'}")
        lines.append(f"QQ login: {'yes' if self.manager._qq_logged_in else 'no'}")
        lines.append(f"NapCat dir: {self.manager.napcat_dir}")
        return "\n".join(lines)

    async def terminate(self):
        for attr in ("_auto_start_task", "_post_install_task"):
            task = getattr(self, attr, None)
            if task and not task.done():
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass
        try:
            await self.manager.stop(self._saved_sudo_password())
        except Exception as e:
            logger.error(f"Stop NapCat failed: {e}")
            try:
                self.manager.cleanup()
            except Exception:
                pass

    @property
    def plugin_dir(self) -> Path:
        if hasattr(self.context, "plugin_dir"):
            return Path(self.context.plugin_dir)
        return Path(__file__).parent
