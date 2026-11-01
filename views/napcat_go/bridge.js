/* NapCat Go — plugin page controller (SnowLuma Go style console). */
(function () {
    "use strict";

    const $ = (id) => document.getElementById(id);
    const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

    const CHANNEL = "astrbot-plugin-page";
    const THEME_KEY = "napcat_go_theme_v2";
    const ONBOARD_KEY = "napcat_go_onboarded_v742";
    const ONBOARD_VERSION = 3;
    const LOCAL_HOSTS = ["127.0.0.1", "localhost", "0.0.0.0", "[::]", "::"];
    const STATUS_INTERVAL = 3000;
    const FAST_INTERVAL = 800;
    const LOG_INTERVAL = 2500;
    const MAX_LOG_LINES = 1200;

    const S = {
        bridge: null,
        status: null,
        releases: [],
        mirrors: [],
        logOffset: 0,
        paused: false,
        busy: new Set(),
        statusTimer: null,
        fastTimer: null,
        logTimer: null,
        configOpen: false,
        downloadOpen: false,
        lastDownloading: false,
        downloadHideTimer: null,
        onboarding: { steps: [], idx: 0, mode: "" },
        context: {},
        theme: "dark",
        themeMode: "auto",
        themeTouched: false,
    };

    // ------------------------------------------------------------------
    // Theme: AstrBot injects data-theme and syncs it through the bridge.
    // Follow it by default; the config switch pins an explicit theme.
    // ------------------------------------------------------------------

    const THEME_MODES = ["auto", "light", "dark"];

    function storedTheme() {
        try {
            const value = localStorage.getItem(THEME_KEY);
            if (THEME_MODES.includes(value)) return value;
        } catch (error) { /* sandboxed iframe */ }
        return "";
    }

    function astrbotTheme() {
        const context = (S.bridge && typeof S.bridge.getContext === "function"
            ? S.bridge.getContext()
            : null) || S.context || {};
        if (typeof context.isDark === "boolean") return context.isDark ? "dark" : "light";
        const attr = document.documentElement.getAttribute("data-theme");
        return attr === "light" ? "light" : "dark";
    }

    function applyTheme(mode, persist) {
        const wanted = THEME_MODES.includes(mode) ? mode : "auto";
        const effective = wanted === "auto" ? astrbotTheme() : wanted;
        S.themeMode = wanted;
        S.theme = effective;
        document.documentElement.setAttribute("data-theme", effective);
        const toggle = $("cfg-light-mode");
        if (toggle) toggle.checked = effective === "light";
        const follow = $("link-follow-theme");
        if (follow) follow.style.display = wanted === "auto" ? "none" : "";
        if (persist) {
            try { localStorage.setItem(THEME_KEY, wanted); } catch (error) { /* ignore */ }
        }
    }

    applyTheme(storedTheme() || "auto");


    // ------------------------------------------------------------------
    // Bridge: use AstrBot's SDK when injected, otherwise speak the same
    // postMessage protocol ourselves.
    // ------------------------------------------------------------------
    function createFallbackBridge() {
        const origin = window.location.origin;
        const pending = new Map();
        let seq = 0;
        let context = null;
        let resolveReady;
        const ready = new Promise((resolve) => { resolveReady = resolve; });

        window.addEventListener("message", (event) => {
            if (event.source !== window.parent) return;
            if (origin !== "null" && event.origin !== origin && event.origin !== "null") return;
            const message = event.data;
            if (!message || message.channel !== CHANNEL) return;
            if (message.kind === "context") {
                context = message.context || {};
                if (resolveReady) { resolveReady(context); resolveReady = null; }
                return;
            }
            if (message.kind === "response") {
                const waiter = pending.get(message.requestId);
                if (!waiter) return;
                pending.delete(message.requestId);
                if (message.ok) waiter.resolve(message.data);
                else waiter.reject(new Error(message.error || "插件后台请求失败"));
            }
        });

        function send(kind, payload) {
            try {
                window.parent.postMessage(
                    { channel: CHANNEL, kind, ...(payload || {}) },
                    origin === "null" ? "*" : origin
                );
            } catch (error) { /* iframe detached */ }
        }

        function request(action, payload, timeoutMs) {
            return new Promise((resolve, reject) => {
                const requestId = "ncgo_" + (++seq);
                const timer = setTimeout(() => {
                    pending.delete(requestId);
                    reject(new Error("请求超时：仪表盘没有响应"));
                }, timeoutMs || 20000);
                pending.set(requestId, {
                    resolve: (value) => { clearTimeout(timer); resolve(value); },
                    reject: (error) => { clearTimeout(timer); reject(error); },
                });
                send("request", { requestId, action, ...payload });
            });
        }

        send("ready");
        setTimeout(() => { if (resolveReady) { resolveReady(context || {}); resolveReady = null; } }, 3500);

        return {
            ready: () => ready,
            getContext: () => context,
            apiGet: (endpoint, params) => request("api:get", { endpoint, params }),
            apiPost: (endpoint, body, timeoutMs) => request("api:post", { endpoint, body }, timeoutMs),
            onContext: (handler) => { if (typeof handler === "function" && context) handler(context); },
        };
    }

    async function resolveBridge() {
        const existing = window.AstrBotPluginPage || window.AstrBotPluginView;
        if (existing) return existing;
        await sleep(400);
        const late = window.AstrBotPluginPage || window.AstrBotPluginView;
        if (late) return late;
        console.warn("[napcat] AstrBot bridge SDK not injected; using built-in bridge");
        const fallback = createFallbackBridge();
        if (!window.AstrBotPluginPage) window.AstrBotPluginPage = fallback;
        return fallback;
    }

    const apiGet = (endpoint, params) => S.bridge.apiGet(endpoint, params);
    const apiPost = (endpoint, body, timeoutMs) => S.bridge.apiPost(endpoint, body || {}, timeoutMs);

    // ------------------------------------------------------------------
    // Helpers
    // ------------------------------------------------------------------
    function esc(value) {
        return String(value == null ? "" : value)
            .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
            .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
    }

    function shorten(text, limit) {
        const raw = String(text == null ? "" : text);
        return raw.length > (limit || 240) ? raw.slice(0, limit || 240) + "…" : raw;
    }

    function setValue(id, text, kind) {
        const node = $(id);
        if (!node) return;
        node.textContent = String(text == null || text === "" ? "-" : text);
        const mono = (id === "s-webui" || id === "s-token") ? " mono-small" : "";
        node.className = "value" + mono + (kind ? " " + kind : "");
    }

    function toast(message, kind, duration) {
        const node = $("toast");
        if (!node) return;
        node.textContent = message;
        node.className = "toast visible" + (kind ? " " + kind : "");
        clearTimeout(toast._timer);
        toast._timer = setTimeout(() => { node.className = "toast" + (kind ? " " + kind : ""); }, duration || 3000);
    }

    function confirmDialog(message, okLabel) {
        return new Promise((resolve) => {
            const mask = $("confirm-mask");
            if (!mask) { resolve(window.confirm(message)); return; }
            $("confirm-msg").textContent = message;
            $("confirm-ok").textContent = okLabel || "确定";
            mask.classList.add("visible");
            const done = (value) => {
                mask.classList.remove("visible");
                $("confirm-ok").removeEventListener("click", onOk);
                $("confirm-cancel").removeEventListener("click", onCancel);
                resolve(value);
            };
            const onOk = () => done(true);
            const onCancel = () => done(false);
            $("confirm-ok").addEventListener("click", onOk);
            $("confirm-cancel").addEventListener("click", onCancel);
        });
    }

    function copyText(text) {
        try {
            if (navigator.clipboard && navigator.clipboard.writeText) {
                navigator.clipboard.writeText(text).then(
                    () => toast("已复制到剪贴板", "ok", 1600),
                    () => fallbackCopy(text)
                );
                return;
            }
        } catch (error) { /* fall through */ }
        fallbackCopy(text);
    }

    function fallbackCopy(text) {
        try {
            const area = document.createElement("textarea");
            area.value = text;
            area.style.position = "fixed";
            area.style.opacity = "0";
            document.body.appendChild(area);
            area.select();
            document.execCommand("copy");
            area.remove();
            toast("已复制到剪贴板", "ok", 1600);
        } catch (error) {
            toast("复制失败，请手动选择文本", "err", 2600);
        }
    }

    // Remote AstrBot visitors must not be sent to their own 127.0.0.1.
    function rewriteUrlForCurrentHost(url) {
        if (!url) return url;
        const currentHost = window.location.hostname;
        if (!currentHost) return url;
        try {
            const parsed = new URL(url);
            if (LOCAL_HOSTS.includes(parsed.hostname)) parsed.hostname = currentHost;
            return parsed.toString();
        } catch (error) {
            return String(url).replace(/127\.0\.0\.1|localhost|0\.0\.0\.0|\[::\]/g, currentHost);
        }
    }

    // ------------------------------------------------------------------
    // One-shot sudo password: RSA-encrypted in the browser, never stored.
    // ------------------------------------------------------------------
    let sudoPubKey = null;

    async function sudoPublicKey() {
        if (sudoPubKey) return sudoPubKey;
        const data = await apiGet("sudo/key");
        if (!data || !data.n || !data.e) throw new Error("无法获取 sudo 公钥");
        sudoPubKey = {
            n: BigInt("0x" + data.n),
            e: BigInt("0x" + data.e),
            size: Number(data.size) || Math.ceil(data.n.length / 2),
        };
        return sudoPubKey;
    }

    function modPow(base, exp, mod) {
        let result = 1n;
        base %= mod;
        while (exp > 0n) {
            if (exp & 1n) result = (result * base) % mod;
            exp >>= 1n;
            base = (base * base) % mod;
        }
        return result;
    }

    async function encryptSudo(plain) {
        const key = await sudoPublicKey();
        const message = new TextEncoder().encode(plain);
        const size = key.size;
        if (message.length > size - 11) throw new Error("sudo 密码过长");
        const padLength = size - message.length - 3;
        const pad = new Uint8Array(padLength);
        crypto.getRandomValues(pad);
        for (let i = 0; i < padLength; i += 1) {
            if (pad[i] === 0) pad[i] = 1;
        }
        const block = new Uint8Array(size);
        block[0] = 0;
        block[1] = 2;
        block.set(pad, 2);
        block[2 + padLength] = 0;
        block.set(message, 3 + padLength);

        let value = 0n;
        for (let i = 0; i < block.length; i += 1) value = (value << 8n) | BigInt(block[i]);
        const cipher = modPow(value, key.e, key.n);
        const out = new Uint8Array(size);
        let c = cipher;
        for (let i = size - 1; i >= 0; i -= 1) {
            out[i] = Number(c & 0xffn);
            c >>= 8n;
        }
        let binary = "";
        for (let i = 0; i < out.length; i += 1) binary += String.fromCharCode(out[i]);
        return btoa(binary);
    }

    function promptSudo(reason) {
        return new Promise((resolve) => {
            const modal = $("sudo-modal");
            if (!modal) {
                const password = window.prompt((reason || "该操作需要 sudo 权限") + "\n\n请输入 sudo 密码（仅本次使用）：");
                if (!password) { resolve(null); return; }
                encryptSudo(password).then(resolve).catch((error) => {
                    toast("加密失败：" + (error.message || error), "err", 4000);
                    resolve(null);
                });
                return;
            }
            const input = $("sudo-password-input");
            const okBtn = $("btn-sudo-ok");
            const cancelBtn = $("btn-sudo-cancel");
            $("sudo-reason").textContent = reason || "该操作需要 sudo 权限";
            input.value = "";
            modal.classList.add("visible");
            setTimeout(() => input.focus(), 30);

            const cleanup = () => {
                modal.classList.remove("visible");
                okBtn.removeEventListener("click", onOk);
                cancelBtn.removeEventListener("click", onCancel);
                input.removeEventListener("keydown", onKey);
                okBtn.disabled = false;
                okBtn.textContent = "确认";
            };
            const onCancel = () => { cleanup(); resolve(null); };
            const onOk = async () => {
                const password = input.value;
                if (!password) { toast("请输入 sudo 密码", "warn", 2500); input.focus(); return; }
                okBtn.disabled = true;
                okBtn.textContent = "加密中...";
                try {
                    const encrypted = await encryptSudo(password);
                    cleanup();
                    resolve(encrypted);
                } catch (error) {
                    okBtn.disabled = false;
                    okBtn.textContent = "确认";
                    toast("加密失败：" + (error.message || error), "err", 4000);
                }
            };
            const onKey = (event) => {
                if (event.key === "Enter") { event.preventDefault(); onOk(); }
                else if (event.key === "Escape") { event.preventDefault(); onCancel(); }
            };
            okBtn.addEventListener("click", onOk);
            cancelBtn.addEventListener("click", onCancel);
            input.addEventListener("keydown", onKey);
        });
    }

    async function postWithSudo(endpoint, body, reason) {
        const base = body || {};
        let result = await apiPost(endpoint, base);
        if (result && result.need_sudo) {
            const encrypted = await promptSudo(reason || result.message || "该操作需要 sudo 密码");
            if (!encrypted) return result;
            result = await apiPost(endpoint, { ...base, sudo_password_enc: encrypted });
        }
        return result;
    }

    // ------------------------------------------------------------------
    // Status rendering
    // ------------------------------------------------------------------
    function modeLabel(mode) {
        return mode === "docker" ? "Docker（容器化）"
            : mode === "windows" ? "Windows 原生"
            : mode === "native" ? "Linux 本地安装"
            : "自动";
    }

    async function refreshStatus() {
        if (!S.bridge) return;
        try {
            const data = await apiGet("status");
            S.status = data || {};
            renderStatus(S.status);
        } catch (error) {
            console.error("[napcat] 拉取状态失败:", error);
        }
    }

    function renderStatus(d) {
        if (!d) return;
        const configuredMode = d.config && d.config.ui_theme;
        if (!S.themeTouched && THEME_MODES.includes(configuredMode)) {
            applyTheme(configuredMode);
        }
        const deploy = d.deploy_mode || "auto";
        const running = !!d.running;
        const installed = d.napcat_installed !== false;
        const subtitle = $("subtitle");
        if (subtitle) {
            subtitle.textContent = installed
                ? `部署方式：${modeLabel(deploy)} · NapCat ${running ? "运行中" : "未运行"}`
                    + (d.qq_version ? ` · QQ ${d.qq_version}` : "")
                : `部署方式：${modeLabel(deploy)} · NapCat 未安装`;
        }

        setValue("s-run", !installed ? "未安装" : running ? "运行中" : "未运行",
            !installed ? "warn" : running ? "ok" : "warn");
        if (d.qq_version) {
            const compat = d.qq_version_compatible === false;
            setValue("s-version", d.qq_version + (compat ? "（不兼容）" : ""), compat ? "err" : "ok");
        } else {
            setValue("s-version", d.qq_installed ? "已安装" : "未安装", d.qq_installed ? "ok" : "warn");
        }
        setValue("s-mode", deploy.toUpperCase(), deploy === "docker" && d.docker_available === false ? "err" : "");
        setValue("s-webui", d.napcat_webui_url || "未就绪", d.napcat_webui_url ? "ok" : "");
        if (d.qq_logged_in) {
            setValue("s-login", d.qq_nickname || (d.qq_user_id ? "QQ " + d.qq_user_id : "已登录"), "ok");
        } else {
            setValue("s-login", "未登录", "warn");
        }
        setValue("s-uin", d.qq_user_id || d.napcat_qq_number || "-");
        setValue("s-bot", d.astrbot_bot_found ? (d.astrbot_bot_id || "已找到") : "未找到", d.astrbot_bot_found ? "ok" : "err");
        setValue("s-bot-port", d.astrbot_bot_port || "-", d.astrbot_bot_found ? "ok" : "");
        setValue("s-port", d.napcat_port || "-");
        setValue("s-token", d.napcat_token || "无", d.napcat_token ? "" : "warn");
        setValue("s-sync", d.last_sync_time || "未同步", d.last_sync_ok ? "ok" : (d.last_sync_time ? "err" : ""));
        const plugin = d.plugin || {};
        setValue("s-plugin", plugin.version || "-", "");
        setValue("s-napcat-dir", d.config_dir || d.napcat_data_dir || "未找到", (d.config_dir || d.napcat_data_dir) ? "" : "err");
        setValue("s-astrbot-path", d.astrbot_config_path || "未找到", d.astrbot_config_path ? "" : "err");

        updatePowerButton(d);
        const syncButton = $("btn-sync");
        if (syncButton) syncButton.disabled = !d.running || !d.astrbot_bot_found;
        renderAccounts(d);
        renderProcesses(d);
        updateDownloadPanel(d.download_state);
        updateGuide(d);
        if (!S.configOpen) fillConfig(d.config || {});
    }

    function renderAccounts(d) {
        const host = $("account-list");
        if (!host) return;
        if (!d.qq_logged_in) {
            host.innerHTML = '<div class="empty-box">还没有账号上线。请打开 WebUI 完成 QQ 登录。</div>';
            return;
        }
        const uin = d.qq_user_id || d.napcat_qq_number || "";
        const nickname = d.qq_nickname || "QQ 已登录";
        host.innerHTML = '<div class="proc-row">' +
            '<div class="who"><b>' + esc(uin || "-") + "</b><span>" + esc(nickname) + "</span></div>" +
            '<div class="acts"><span class="tag ok">在线</span></div></div>';
    }

    function renderProcesses(d) {
        const host = $("qq-process-list");
        if (!host) return;
        const processes = d.qq_processes || [];
        if (!processes.length) {
            host.innerHTML = '<div class="empty-box">' + (d.running
                ? "NapCat 正在运行，尚未发现 QQ 进程。"
                : "NapCat 未运行，暂无可显示的 QQ 进程。") + "</div>";
            return;
        }
        host.innerHTML = processes.map((info) => {
            const pid = Number(info.pid || 0);
            const name = info.name || "NapCat / QQ";
            return '<div class="proc-row">' +
                '<div class="who"><b>' + (pid ? "PID " + pid : "Docker") + "</b><span>" + esc(name) + "</span></div>" +
                '<div class="acts"><span class="tag ' + (info.online === false ? "warn" : "ok") + '">' +
                (info.online === false ? "已停止" : "运行中") + "</span></div></div>";
        }).join("");
    }

    function updatePowerButton(d) {
        const button = $("btn-toggle-power");
        const label = $("power-label");
        if (!button || !label) return;
        const download = d.download_state || {};
        if (download.downloading) {
            button.disabled = true;
            button.classList.remove("power-on", "power-off");
            button.classList.add("power-downloading");
            label.textContent = download.percent > 0 ? `下载中 ${download.percent}%` : "准备下载...";
            return;
        }
        button.disabled = false;
        button.classList.remove("power-downloading");
        if (d.napcat_installed === false) {
            button.classList.remove("power-off");
            button.classList.add("power-on");
            label.textContent = "安装 NapCat";
            return;
        }
        if (d.running) {
            button.classList.remove("power-on");
            button.classList.add("power-off");
            label.textContent = "停止 NapCat";
        } else {
            button.classList.remove("power-off");
            button.classList.add("power-on");
            label.textContent = "启动 NapCat";
        }
    }

    function renderGuide(d) {
        const guide = $("guide");
        if (!guide) return;
        if (d.napcat_installed === false) {
            guide.className = "guide warn visible";
            guide.innerHTML = `<div style="font-weight:600;margin-bottom:4px;">尚未安装 NapCat</div>
                <div>点上方「安装 NapCat」打开下载与安装，选择版本后会自动下载、安装并启动。</div>`;
            return;
        }
        if (d.astrbot_bot_found && d.napcat_config_file && d.last_sync_ok) {
            guide.className = "guide success visible";
            guide.innerHTML = `<div style="font-weight:600;margin-bottom:4px;">配置已同步</div>
                <div>反向 WS：<code>ws://${esc(d.astrbot_bot_host || "127.0.0.1")}:${esc(d.astrbot_bot_port || "")}/ws/</code></div>`;
            return;
        }
        if (!d.astrbot_bot_found) {
            guide.className = "guide visible";
            guide.innerHTML = `<div style="font-weight:600;margin-bottom:4px;">先在 AstrBot 创建 OneBot v11 机器人</div>
                <div>AstrBot 左侧栏 → 机器人 → 创建机器人 → OneBot v11，保存后回到这里点「立即同步」。</div>`;
            return;
        }
        if (!d.napcat_config_dir) {
            guide.className = "guide warn visible";
            guide.innerHTML = `<div style="font-weight:600;margin-bottom:4px;">未找到 NapCat 配置目录</div>
                <div>点右上角「配置」，手动填写 NapCat 的 config 目录路径。</div>`;
            return;
        }
        if (d.last_sync_time && !d.last_sync_ok) {
            guide.className = "guide error visible";
            guide.innerHTML = `<div style="font-weight:600;margin-bottom:4px;">同步失败</div>
                <div>${esc(shorten(d.last_sync_msg || "未知错误", 500))}</div>`;
            return;
        }
        guide.className = "guide";
    }

    function updateGuide(d) { requestAnimationFrame(() => renderGuide(d)); }

    function updateDownloadPanel(dl) {
        const panel = $("download-panel");
        if (!panel) return;
        if (!dl) {
            panel.classList.remove("visible", "done", "failed");
            return;
        }
        const phase = dl.phase || "idle";
        const active = !!dl.downloading;
        const done = phase === "done";
        const failed = phase === "failed";
        if (!active && !done && !failed) {
            panel.classList.remove("visible", "done", "failed");
            return;
        }
        panel.classList.add("visible");
        panel.classList.toggle("done", done);
        panel.classList.toggle("failed", failed);

        const title = $("dl-title-text");
        if (title) {
            if (active) {
                title.textContent = phase === "extracting" ? "正在解压 NapCat.Shell.zip"
                    : phase === "installing" ? "正在安装 NapCat"
                    : "正在下载 NapCat.Shell.zip";
            } else {
                title.textContent = dl.message || (done ? "完成" : "失败");
            }
        }
        const mirror = $("dl-mirror");
        if (mirror) {
            if (active && dl.current_mirror && dl.total_mirrors > 0) {
                mirror.textContent = `${dl.current_index}/${dl.total_mirrors} · ${dl.current_mirror}`;
            } else if (dl.current_mirror) {
                mirror.textContent = dl.current_mirror;
            } else {
                mirror.textContent = "";
            }
        }
        let percent = Number(dl.percent || 0);
        if (done) percent = 100;
        if (failed) percent = 0;
        const fill = $("dl-progress-fill");
        if (fill) fill.style.width = percent + "%";
        const pct = $("dl-percent");
        if (pct) pct.textContent = percent + "%";
        const size = $("dl-size");
        if (size) {
            if (dl.total_mb > 0) size.textContent = `${Number(dl.downloaded_mb || 0).toFixed(1)} / ${Number(dl.total_mb).toFixed(1)} MB`;
            else if (dl.downloaded_mb > 0) size.textContent = `${Number(dl.downloaded_mb).toFixed(1)} MB`;
            else size.textContent = "";
        }
        const speed = $("dl-speed");
        if (speed) speed.textContent = active && dl.speed_kbps > 0 ? `${Number(dl.speed_kbps).toFixed(0)} KB/s` : "";

        if (S.downloadHideTimer) { clearTimeout(S.downloadHideTimer); S.downloadHideTimer = null; }
        if (done || failed) {
            S.downloadHideTimer = setTimeout(() => panel.classList.remove("visible", "done", "failed"), 5000);
        }
        if (active && !S.fastTimer) startFastPolling();
        if (!active && S.fastTimer) stopFastPolling();
    }

    function startFastPolling() {
        if (S.fastTimer) return;
        S.fastTimer = setInterval(async () => {
            if (!S.bridge) return;
            try {
                const data = await apiGet("status");
                S.status = data || {};
                renderStatus(S.status);
                if (!(data && data.download_state && data.download_state.downloading)) stopFastPolling();
            } catch (error) { /* ignore */ }
        }, FAST_INTERVAL);
    }

    function stopFastPolling() {
        if (S.fastTimer) { clearInterval(S.fastTimer); S.fastTimer = null; refreshStatus(); }
    }

    // ------------------------------------------------------------------
    // Logs
    // ------------------------------------------------------------------
    async function refreshLogs() {
        if (!S.bridge || S.paused) return;
        try {
            const data = await apiGet("logs", { since: S.logOffset });
            const box = $("log-container");
            if (!box) return;
            if (data && Array.isArray(data.lines) && data.lines.length) {
                box.textContent += (box.textContent ? "\n" : "") + data.lines.join("\n");
                const lines = box.textContent.split("\n");
                if (lines.length > MAX_LOG_LINES) box.textContent = lines.slice(-MAX_LOG_LINES).join("\n");
                box.scrollTop = box.scrollHeight;
                S.logOffset = data.total || (S.logOffset + data.lines.length);
            }
            const count = $("log-count");
            if (count) count.textContent = `${(data && data.total) || 0} 行`;
        } catch (error) {
            console.error("[napcat] 拉取日志失败:", error);
        }
    }

    function clearLogs() {
        const box = $("log-container");
        if (box) box.textContent = "";
        S.logOffset = 0;
        toast("日志已清屏", "", 1500);
    }

    // ------------------------------------------------------------------
    // Actions
    // ------------------------------------------------------------------
    async function togglePower() {
        const status = S.status || {};
        const download = status.download_state || {};
        if (download.downloading) {
            toast("NapCat 正在下载或安装，请稍候", "warn", 2200);
            return;
        }
        if (!status.running && status.napcat_installed === false) {
            toast("未检测到 NapCat，已打开下载与安装", "warn", 2800);
            openDownload();
            return;
        }
        const running = !!status.running;
        if (!running && status.is_linux && status.qq_version_compatible === false) {
            if (!(await confirmDialog(
                "当前 LinuxQQ 版本不在 NapCat 兼容范围内，启动时可能自动卸载并重装 QQ，存在数据丢失风险。确定继续？",
                "继续启动"
            ))) return;
        }
        if (running) {
            if (!(await confirmDialog("停止 NapCat 会断开 AstrBot 的连接，确定继续？", "停止"))) return;
        }
        const endpoint = running ? "stop" : "start";
        try {
            const result = await postWithSudo(
                endpoint, {},
                running ? "停止 NapCat 需要 sudo 密码（仅本次使用）" : "启动 NapCat 需要 sudo 密码（仅本次使用）"
            );
            if (result && result.ok) toast(running ? "停止指令已发送" : "启动指令已发送", "ok", 2200);
            else if (result && result.need_install) {
                toast(result.message || "未检测到 NapCat，已打开下载与安装", "warn", 3000);
                openDownload();
            } else if (result && result.need_sudo) toast("已取消，未发送指令", "warn", 2000);
            else toast(`操作失败：${(result && result.message) || "未知错误"}`, "err", 4000);
            await refreshStatus();
        } catch (error) {
            toast("操作失败：" + (error.message || error), "err", 4000);
        }
    }

    async function doSync() {
        if (!(await confirmDialog("将把 AstrBot 的 OneBot v11 配置写入 NapCat，继续？", "同步"))) return;
        try {
            const result = await apiPost("sync");
            if (result && result.ok) toast("同步成功", "ok", 2200);
            else toast("同步失败：" + ((result && result.message) || "未知错误"), "err", 4500);
            await refreshStatus();
        } catch (error) {
            toast("同步失败：" + (error.message || error), "err", 4000);
        }
    }

    async function doRestart() {
        const restartRisk = (S.status && S.status.is_linux && S.status.qq_version_compatible === false)
            ? "\n\n注意：当前 LinuxQQ 版本不在 NapCat 兼容范围内，重启过程可能自动卸载并重装 QQ，存在数据丢失风险。"
            : "";
        if (!(await confirmDialog("确定要重启 NapCat 吗？" + restartRisk, "重启"))) return;
        try {
            const result = await postWithSudo("restart", {}, "重启 NapCat 需要 sudo 密码（仅本次使用）");
            if (result && result.ok) toast("重启指令已发送", "ok", 2200);
            else if (result && result.need_sudo) toast("已取消", "warn", 1600);
            else toast("操作失败：" + ((result && result.message) || ""), "err", 4000);
            await refreshStatus();
        } catch (error) { toast("操作失败：" + (error.message || error), "err", 4000); }
    }

    async function doCleanup() {
        if (!(await confirmDialog("将强制结束所有 NapCat / QQ / Xvfb 相关进程，继续？", "清理"))) return;
        try {
            const result = await postWithSudo("cleanup", {}, "清理残留进程需要 sudo 密码（仅本次使用）");
            if (result && result.ok) toast("已清理残留进程", "ok", 2200);
            else if (result && result.need_sudo) toast("已取消", "warn", 1600);
            else toast("清理失败：" + ((result && result.message) || ""), "err", 4000);
            await refreshStatus();
        } catch (error) { toast("清理失败：" + (error.message || error), "err", 4000); }
    }

    async function doRefresh() {
        if (!(await confirmDialog("重新探测 NapCat 安装路径与配置？", "探测"))) return;
        try {
            await apiPost("refresh");
            toast("已重新探测", "ok", 1600);
            await refreshStatus();
        } catch (error) { toast("探测失败：" + (error.message || error), "err", 4000); }
    }

    function openWebUI() {
        const url = S.status && S.status.napcat_webui_url ? rewriteUrlForCurrentHost(S.status.napcat_webui_url) : "";
        if (!url) { toast("WebUI 地址尚未就绪，请先启动 NapCat 并等待日志出现 WebUI URL", "warn", 4000); return; }
        const field = $("webui-url-field");
        if (field) field.value = url;
        let token = "";
        try { token = new URL(url).searchParams.get("token") || ""; } catch (error) { /* ignore */ }
        if (!token && S.status) token = S.status.napcat_token || "";
        const tokenField = $("webui-token-field");
        if (tokenField) tokenField.value = token;
        const link = $("webui-open-link");
        if (link) link.href = url;
        const modal = $("webui-modal");
        if (modal) modal.classList.add("visible");
        window.open(url, "_blank", "noopener,noreferrer");
    }

    // ------------------------------------------------------------------
    // Config
    // ------------------------------------------------------------------
    function fillConfig(cfg) {
        cfg = cfg || {};
        const set = (id, value) => { const node = $(id); if (node) node.value = value == null ? "" : value; };
        set("cfg-deploy", cfg.deploy_mode || "auto");
        set("cfg-docker-image", cfg.docker_image || "mlikiowa/napcat-docker");
        set("cfg-docker-tag", cfg.docker_tag || "latest");
        set("cfg-docker-container", cfg.docker_container_name || "napcat-go");
        set("cfg-napcat-port", cfg.napcat_port || 6099);
        set("cfg-qq-number", cfg.qq_number || "");
        set("cfg-napcat-dir", cfg.napcat_config_dir || "");
        set("cfg-astrbot-path", cfg.astrbot_config_path || "");
        const autoStart = $("cfg-auto-start");
        if (autoStart) autoStart.checked = cfg.auto_start !== false;
        const autoStartAfterInstall = $("cfg-auto-start-after-install");
        if (autoStartAfterInstall) autoStartAfterInstall.checked = cfg.auto_start_after_install !== false;
        const autoSync = $("cfg-auto-sync");
        if (autoSync) autoSync.checked = cfg.auto_sync !== false;
        const sudoInput = $("cfg-sudo-password");
        if (sudoInput) {
            sudoInput.value = "";
            const saved = cfg.sudo_password_saved === true
                || !!(S.status && S.status.sudo_password_saved);
            sudoInput.placeholder = saved ? "已保存，留空则不修改" : "留空则不自动提权";
        }
    }

    async function openConfig() {
        S.configOpen = true;
        const modal = $("config-modal");
        if (modal) modal.classList.add("visible");
        fillConfig((S.status && S.status.config) || {});
        try {
            const [statusRes, cfgRes] = await Promise.allSettled([apiGet("status"), apiGet("config")]);
            if (statusRes.status === "fulfilled") S.status = statusRes.value || S.status;
            const cfg = cfgRes.status === "fulfilled" ? cfgRes.value : ((S.status && S.status.config) || {});
            fillConfig(cfg || {});
            if (cfg && THEME_MODES.includes(cfg.ui_theme)) {
                applyTheme(cfg.ui_theme);
            }
        } catch (error) {
            fillConfig((S.status && S.status.config) || {});
        }
    }

    function closeConfig() {
        S.configOpen = false;
        const modal = $("config-modal");
        if (modal) modal.classList.remove("visible");
    }

    async function saveConfig() {
        const val = (id) => (($(id) || {}).value || "").trim();
        const sudoPlain = (($("cfg-sudo-password") || {}).value || "").trim();
        if (sudoPlain && !(await confirmDialog(
            "将把 sudo 密码加密保存到本机插件配置，之后启动、安装、同步会自动使用它。确定保存？",
            "保存密码"
        ))) return;
        let sudoEnc = "";
        if (sudoPlain) {
            try {
                sudoEnc = await encryptSudo(sudoPlain);
            } catch (error) {
                toast("sudo 密码加密失败：" + (error.message || error), "err", 4200);
                return;
            }
        }
        const payload = {
            deploy_mode: val("cfg-deploy") || "auto",
            docker_image: val("cfg-docker-image"),
            docker_tag: val("cfg-docker-tag"),
            docker_container_name: val("cfg-docker-container"),
            napcat_port: Number(val("cfg-napcat-port") || 6099),
            qq_number: val("cfg-qq-number"),
            napcat_config_dir: val("cfg-napcat-dir"),
            astrbot_config_path: val("cfg-astrbot-path"),
            auto_start: !!($("cfg-auto-start") || {}).checked,
            auto_start_after_install: !!($("cfg-auto-start-after-install") || {}).checked,
            auto_sync: !!($("cfg-auto-sync") || {}).checked,
        };
        if (sudoEnc) payload.sudo_password_enc = sudoEnc;
        try {
            const result = await postWithSudo(
                "config/save", payload,
                "保存并同步配置需要 sudo 密码（用于修复 NapCat 配置目录权限，仅本次使用）"
            );
            if (result && result.need_sudo) {
                // Backend asks for sudo because sync hit permission errors.
                toast("已取消保存（需要 sudo 权限修复配置目录）", "warn", 3000);
                return;
            }
            closeConfig();
            const sudoInput = $("cfg-sudo-password");
            if (sudoInput) sudoInput.value = "";
            await refreshStatus();
            if (result && result.sync_ok) toast("配置已保存，同步成功", "ok", 2600);
            else if (result && result.sync_msg) toast(`已保存，同步失败：${shorten(result.sync_msg, 160)}`, "err", 5000);
            else toast("配置已保存", "ok", 2000);
        } catch (error) {
            toast("保存失败：" + (error.message || error), "err", 4000);
        }
    }

    async function scanPaths() {
        const box = $("scan-results");
        if (box) box.innerHTML = '<div class="mini-note">正在扫描...</div>';
        try {
            const result = await apiPost("scan");
            const candidates = (result && result.candidates) || [];
            if (!box) return;
            if (!candidates.length) {
                box.innerHTML = '<div class="scan-result-empty">未找到候选路径，请手动填写。</div>';
                return;
            }
            box.innerHTML = `<div class="mini-note" style="margin-bottom:6px;">找到 ${candidates.length} 个候选，点击选择：</div>`
                + candidates.map((item, index) => `<div class="scan-result-item" data-index="${index}">
                    <div class="dir">目录：${esc(item.dir)}</div>
                    <div class="file">文件：${esc(item.file_name || item.file || "")}</div>
                </div>`).join("");
            box.querySelectorAll(".scan-result-item").forEach((node) => {
                node.addEventListener("click", () => {
                    const item = candidates[Number(node.dataset.index)];
                    const input = $("cfg-napcat-dir");
                    if (input && item) input.value = item.dir;
                    box.querySelectorAll(".scan-result-item").forEach((n) => n.classList.remove("selected"));
                    node.classList.add("selected");
                    toast("已选择，保存配置后生效", "ok", 2000);
                });
            });
        } catch (error) {
            if (box) box.innerHTML = `<div class="scan-result-empty">扫描失败：${esc(error.message || error)}</div>`;
        }
    }

    async function installLinuxDeps() {
        try {
            const result = await postWithSudo(
                "linux/install-deps", {},
                "安装 Linux 依赖需要 sudo 密码（仅本次使用）"
            );
            if (result && result.ok) toast(result.message || "Linux 依赖已安装", "ok", 3500);
            else if (result && result.need_sudo) toast("已取消", "warn", 1600);
            else toast("安装失败：" + ((result && result.message) || "请查看日志"), "err", 5000);
        } catch (error) { toast("安装失败：" + (error.message || error), "err", 4000); }
    }

    // ------------------------------------------------------------------
    // Download
    // ------------------------------------------------------------------
    async function openDownload() {
        S.downloadOpen = true;
        const modal = $("download-modal");
        if (modal) modal.classList.add("visible");
        const status = S.status || {};
        const current = $("dl-current");
        if (current) {
            const installed = status.napcat_installed ? "已安装" : "未安装";
            const qq = status.qq_version ? `QQ ${status.qq_version}` : "QQ 未检测到";
            current.textContent = `NapCat ${installed} · ${qq} · 部署方式 ${modeLabel(status.deploy_mode || "auto")}`;
        }
        let cfg = null;
        let releases = null;
        try { cfg = await apiGet("config"); } catch (error) { /* keep defaults */ }
        try { releases = await apiGet("releases"); } catch (error) { /* keep defaults */ }
        S.mirrors = (cfg && cfg.download_mirrors) || [];
        S.releases = (releases && releases.releases) || [];
        const mirrorInput = $("dl-mirror-input");
        if (mirrorInput) mirrorInput.value = (cfg && cfg.napcat_download_mirror) || "";
        populateVersions((cfg && cfg.napcat_version) || "latest");
        renderMirrorMenu();
    }

    function closeDownload() {
        S.downloadOpen = false;
        setMirrorMenu(false);
        const modal = $("download-modal");
        if (modal) modal.classList.remove("visible");
    }

    function populateVersions(selected) {
        const select = $("dl-version");
        if (!select) return;
        select.innerHTML = "";
        const latest = document.createElement("option");
        latest.value = "latest";
        latest.textContent = "latest（最新版）";
        select.appendChild(latest);
        S.releases.forEach((release) => {
            const option = document.createElement("option");
            option.value = release.tag;
            option.textContent = release.tag + (release.prerelease ? "（预发布）" : "")
                + (release.published_at ? " · " + String(release.published_at).slice(0, 10) : "");
            select.appendChild(option);
        });
        const wanted = selected || "latest";
        select.value = Array.from(select.options).some((o) => o.value === wanted) ? wanted : "latest";
    }

    function renderMirrorMenu() {
        const menu = $("dl-mirror-menu");
        if (!menu) return;
        const current = (($("dl-mirror-input") || {}).value || "").trim();
        const items = S.mirrors.length ? S.mirrors : [];
        if (!items.length) {
            menu.innerHTML = '<div class="mini-note" style="padding:8px;">暂无内置镜像</div>';
            return;
        }
        menu.innerHTML = items.map((url) => `<button class="mirror-option${url === current ? " selected" : ""}" type="button" data-url="${esc(url)}">
            <span class="mirror-url">${esc(url)}</span>
            <span class="mirror-meta">${url === current ? '<span class="mirror-check">✓</span>' : ""}</span>
        </button>`).join("");
        menu.querySelectorAll(".mirror-option").forEach((node) => {
            node.addEventListener("click", () => {
                const input = $("dl-mirror-input");
                if (input) input.value = node.dataset.url || "";
                setMirrorMenu(false);
                renderMirrorMenu();
                saveDownloadOptions({ napcat_download_mirror: (input && input.value.trim()) || "" }, false);
            });
        });
    }

    function setMirrorMenu(open) {
        const picker = $("dl-mirror-picker");
        const menu = $("dl-mirror-menu");
        const toggle = $("dl-mirror-toggle");
        if (picker) picker.classList.toggle("open", !!open);
        if (menu) menu.classList.toggle("visible", !!open);
        if (toggle) toggle.setAttribute("aria-expanded", open ? "true" : "false");
    }

    let mirrorTimer = null;
    function queueMirrorSave() {
        clearTimeout(mirrorTimer);
        mirrorTimer = setTimeout(() => {
            const value = (($("dl-mirror-input") || {}).value || "").trim();
            saveDownloadOptions({ napcat_download_mirror: value }, false);
        }, 500);
    }

    async function saveDownloadOptions(patch, notify) {
        try {
            await apiPost("download/options", patch);
            if (S.status && S.status.config) Object.assign(S.status.config, patch);
            if (notify) toast("下载设置已保存", "ok", 1600);
        } catch (error) {
            if (notify) toast("保存下载设置失败：" + (error.message || error), "err", 3500);
        }
    }

    async function doInstall() {
        if (S.status && S.status.running) {
            toast("NapCat 正在运行，请先停止后再安装新版本", "err", 4000);
            return;
        }
        const version = ($("dl-version") || {}).value || "latest";
        const installed = !(S.status && S.status.napcat_installed === false);
        const message = installed
            ? `安装 NapCat 版本「${version}」？旧程序文件会被替换，config 配置会保留。`
            : `将下载并安装 NapCat 版本「${version}」。继续？`;
        const riskNote = (S.status && S.status.is_linux && S.status.qq_version_compatible === false)
            ? "\n\n注意：当前 LinuxQQ 版本不在 NapCat 兼容范围内，安装过程可能自动卸载并重装 QQ，存在数据丢失风险。"
            : "";
        if (!(await confirmDialog(message + riskNote, installed ? "安装" : "开始安装"))) return;
        const button = $("btn-dl-install");
        if (button) { button.disabled = true; button.textContent = "安装中..."; }
        try {
            const result = await postWithSudo(
                "install", { version },
                `安装 NapCat 版本「${version}」需要 sudo 密码（仅本次使用）`
            );
            if (result && result.ok) {
                toast(result.message || "安装完成", "ok", 3200);
                closeDownload();
                startFastPolling();
            } else if (result && result.need_sudo) {
                toast("已取消安装", "warn", 1800);
            } else {
                toast(`安装失败：${(result && result.message) || "未知错误"}`, "err", 5000);
            }
            await refreshStatus();
        } catch (error) {
            toast("安装失败：" + (error.message || error), "err", 4500);
        } finally {
            if (button) { button.disabled = false; button.textContent = "安装 / 更新"; }
        }
    }

    async function doUninstall() {
        if (!(await confirmDialog("卸载 NapCat 会删除程序文件（保留 config 配置），确定继续？", "卸载"))) return;
        try {
            const result = await postWithSudo("uninstall", { purge_qq: false }, "卸载 NapCat 需要 sudo 密码（仅本次使用）");
            if (result && result.ok) toast("已卸载 NapCat", "ok", 2400);
            else if (result && result.need_sudo) toast("已取消", "warn", 1600);
            else toast("卸载失败：" + ((result && result.message) || ""), "err", 4500);
            await refreshStatus();
        } catch (error) { toast("卸载失败：" + (error.message || error), "err", 4000); }
    }

    // ------------------------------------------------------------------
    // Onboarding
    // ------------------------------------------------------------------
    // Comprehensive first-run guide (welcome + prerequisites + UI tour).
    function onboardingSteps() {
        const tour = [
            ["btn-toggle-power", "启动 / 停止", "一键拉起或关闭 NapCat 进程。启动成功后按钮会变成红色「停止」。"],
            ["btn-sync", "立即同步", "把 AstrBot 里的 OneBot v11 配置（端口、Token）写入 NapCat。<b>需要先启动 NapCat 才能同步。</b>"],
            ["btn-open-webui", "打开 WebUI", "启动成功后从这里打开 NapCat WebUI，完成 QQ 扫码登录。这里打开的是 NapCat，不是 AstrBot 后台。"],
            ["s-run", "NapCat 状态", "显示 NapCat 是否正在运行。绿色=运行中，黄色=未运行。"],
            ["s-version", "QQ 版本", "当前 LinuxQQ 版本与兼容性。不兼容时插件会尝试自动安装推荐版本。"],
            ["s-mode", "部署方式", "当前使用 Docker 还是 Linux 本地安装。"],
            ["s-login", "登录状态", "QQ 是否已经扫码登录成功，未登录时 NapCat 收不到消息。"],
            ["s-uin", "QQ 账号", "当前登录的 QQ 号。"],
            ["s-bot", "AstrBot 机器人", "插件从 AstrBot 配置里识别到的 OneBot v11 机器人。"],
            ["s-bot-port", "反向 WS 端口", "AstrBot 反向 WebSocket 监听端口，默认 6199。"],
            ["s-token", "WebUI Token", "NapCat WebUI 的访问 Token，地址里会自动带上。"],
            ["s-sync", "上次同步", "上一次「立即同步」的时间和结果，红色表示失败。"],
            ["s-plugin", "插件版本", "当前安装的 NapCat Go 插件版本。"],
            ["s-napcat-dir", "NapCat 配置目录", "这里应有 onebot11_*.json；路径不对时可在配置里手动填写。"],
            ["s-astrbot-path", "AstrBot 配置", "插件自动探测到的 AstrBot 配置文件路径。"],
            ["btn-restart", "重启 NapCat", "修改配置或同步后，用这里让 NapCat 重新加载。"],
            ["btn-cleanup", "清理残留", "强制结束残留的 NapCat / QQ / Xvfb 进程（会二次确认）。"],
            ["btn-refresh", "重新探测", "部署方式或目录发生变化时，重新探测安装路径和配置。"],
            ["log-container", "实时日志", "安装、启动、下载进度和报错都在这里；失败时先看最后几行。"],
        ];

        const steps = [
            {
                type: "backdrop",
                html:
                    '<span class="ob-step-badge">NapCat Go</span>' +
                    '<div class="ob-welcome-title">欢迎使用<br>NapCat Go</div>' +
                    '<div class="ob-welcome-sub">接下来会带你完成第一次 AstrBot 对接，<br>并逐个认识界面上的按钮和状态。</div>' +
                    '<div class="ob-hint">第一次使用建议跟着引导走一遍，随时可以跳过。</div>'
            },
            {
                type: "backdrop",
                html:
                    '<span class="ob-step-badge">前置 1 / 2</span>' +
                    '<h3>在 AstrBot 中新建机器人</h3>' +
                    '<p>打开 <b>AstrBot 管理后台</b> → <code>机器人</code>：</p>' +
                    '<ul>' +
                    '<li>点击 <b>新建机器人</b>，平台选择 <code>OneBot v11</code>；</li>' +
                    '<li>打开 <b>启用</b>，反向 WebSocket 主机填 <code>0.0.0.0</code>，端口默认 <code>6199</code>；</li>' +
                    '<li>填写一个随机 <b>Token</b>，不要留空。</li>' +
                    '</ul>' +
                    '<img class="ob-shot" src="./assets/astbot-create-bot.png" alt="AstrBot 创建机器人">' +
                    '<div class="ob-shot-cap">AstrBot 新建机器人界面（红框为关键填写项）</div>' +
                    '<div class="ob-warn-box">Token 相当于连接密码，<b>务必填写随机字符串</b>。插件稍后会自动把该 Token 写入 NapCat。</div>'
            },
            {
                type: "backdrop",
                html:
                    '<span class="ob-step-badge">前置 2 / 2</span>' +
                    '<h3>关闭 NapCat 的 SSL 证书</h3>' +
                    '<p>打开 <b>NapCat WebUI</b> → <code>网络配置</code> → <code>编辑网络配置</code>（WebSocket 客户端）：</p>' +
                    '<ul>' +
                    '<li>找到底部的 <b>SSL 证书验证</b>；</li>' +
                    '<li>把它<b>关闭</b>，然后保存。</li>' +
                    '</ul>' +
                    '<img class="ob-shot" src="./assets/napcat-wsclient-ssl.png" alt="NapCat WebSocket 客户端配置">' +
                    '<div class="ob-shot-cap">NapCat WebSocket 客户端编辑界面（底部 SSL 证书验证需关闭）</div>' +
                    '<div class="ob-warn-box">只有关闭 SSL，AstrBot 服务端才能成功安装证书并与 NapCat 建立反向 WebSocket 连接。</div>'
            }
        ];

        tour.forEach(([target, title, desc], index) => {
            steps.push({
                type: "spotlight",
                target,
                html:
                    '<span class="ob-step-badge">界面导览</span>' +
                    "<h3>" + title + "</h3>" +
                    "<p>" + desc + "</p>"
            });
        });

        steps.push({
            type: "spotlight",
            target: "btn-open-webui",
            urgent: true,
            html:
                '<span class="ob-step-badge">FINAL 重点提醒</span>' +
                '<h3 style="color:#ffd98a;">WebUI 与「同步配置」</h3>' +
                '<ul>' +
                '<li><b>不要点错</b>：「打开 WebUI」进入的是 NapCat 网页端，不是 AstrBot 后台；</li>' +
                '<li><b>「立即同步」需要先启动完成</b>，NapCat 未启动时按钮是灰的属于正常现象；</li>' +
                '<li>第一次同步完成后，请再次前往 <code>NapCat WebUI → 网络配置 → 编辑网络配置</code>，<b>关闭 SSL 证书</b>。</li>' +
                "</ul>" +
                '<div class="ob-danger-box"><b>再次提醒：</b>只有关闭 SSL，AstrBot 才能成功安装证书并建立连接。</div>'
        });
        return steps;
    }

    function onboardingSeen() {
        try { if (localStorage.getItem(ONBOARD_KEY) === "1") return true; } catch (error) { /* ignore */ }
        const cfg = (S.status && S.status.config) || {};
        return !!(cfg.onboarding_done && Number(cfg.onboarding_version || 0) >= ONBOARD_VERSION);
    }

    function onboardingApiDone() {
        try { apiPost("onboarding/done").catch(() => {}); } catch (error) { /* ignore */ }
    }

    function finishOnboarding() {
        try { localStorage.setItem(ONBOARD_KEY, "1"); } catch (error) { /* ignore */ }
        finishOnboardingSilently();
        onboardingApiDone();
    }

    function finishOnboardingSilently() {
        const overlay = $("onboarding");
        if (overlay) overlay.classList.remove("visible", "with-backdrop", "with-spotlight");
    }

    function startOnboarding(force) {
        if (!force && onboardingSeen()) return;
        S.onboarding = { steps: onboardingSteps(), idx: 0, rendered: false };
        const overlay = $("onboarding");
        if (!overlay) return;
        overlay.classList.add("visible");
        renderOnboarding();
    }

    function onboardingTargetVisible(target) {
        if (!target) return false;
        if (target.offsetParent === null && target.getClientRects().length === 0) return false;
        const style = window.getComputedStyle(target);
        return style.display !== "none" && style.visibility !== "hidden";
    }

    function positionOnboardingSpotlight() {
        const overlay = $("onboarding");
        const card = $("ob-card");
        const spotlight = $("ob-spotlight");
        const steps = (S.onboarding && S.onboarding.steps) || [];
        let idx = S.onboarding ? S.onboarding.idx : 0;
        if (!overlay || !card || !spotlight) return;
        const step = steps[idx];
        if (!step || !step.target) {
            overlay.classList.remove("with-spotlight");
            return;
        }
        const target = document.getElementById(step.target);
        if (!onboardingTargetVisible(target)) {
            // Skip hidden targets (e.g. Windows-only cards on Linux).
            if (idx < steps.length - 1) {
                S.onboarding.idx = idx + 1;
                renderOnboarding();
            } else {
                overlay.classList.remove("with-spotlight");
            }
            return;
        }
        target.scrollIntoView({ block: "nearest", inline: "nearest" });
        const rect = target.getBoundingClientRect();
        const pad = 6;
        spotlight.style.left = (rect.left - pad) + "px";
        spotlight.style.top = (rect.top - pad) + "px";
        spotlight.style.width = (rect.width + pad * 2) + "px";
        spotlight.style.height = (rect.height + pad * 2) + "px";
        spotlight.style.opacity = "1";
        spotlight.classList.toggle("urgent", !!step.urgent);

        const cardWidth = Math.min(430, window.innerWidth - 32);
        const cardHeight = card.offsetHeight || 300;
        const left = Math.max(16, Math.min(window.innerWidth - cardWidth - 16, rect.left + rect.width / 2 - cardWidth / 2));
        let top;
        if (rect.bottom + cardHeight + 24 < window.innerHeight) {
            top = rect.bottom + 18;
        } else if (rect.top - cardHeight - 18 > 0) {
            top = rect.top - cardHeight - 18;
        } else {
            top = Math.max(16, (window.innerHeight - cardHeight) / 2);
        }
        card.style.left = left + "px";
        card.style.top = top + "px";
        card.style.width = cardWidth + "px";
        card.style.transform = "none";
        overlay.classList.add("with-spotlight");
        overlay.classList.remove("with-backdrop");
    }

    function renderOnboarding() {
        const overlay = $("onboarding");
        const card = $("ob-card");
        const spotlight = $("ob-spotlight");
        if (!overlay || !card) return;
        const steps = (S.onboarding && S.onboarding.steps) || [];
        const idx = S.onboarding ? S.onboarding.idx : 0;
        const step = steps[idx];
        if (!step) { finishOnboarding(); return; }

        const isLast = idx === steps.length - 1;
        card.innerHTML =
            step.html +
            '<div class="ob-dots"><span style="font-size:11px;color:var(--text-muted);font-family:Consolas,monospace;">' +
            (idx + 1) + " / " + steps.length +
            "</span></div>" +
            '<div class="ob-actions">' +
            '<button class="ob-btn skip" data-ob="skip">' + (idx === 0 ? "跳过引导" : "不再提示") + "</button>" +
            '<div class="ob-right">' +
            (idx > 0 ? '<button class="ob-btn" data-ob="prev">上一步</button>' : "") +
            '<button class="ob-btn primary" data-ob="' + (isLast ? "done" : "next") + '">' +
            (isLast ? "开始使用" : "下一步") +
            "</button></div></div>";

        if (step.type === "backdrop") {
            overlay.classList.remove("with-spotlight");
            overlay.classList.add("with-backdrop");
            if (spotlight) spotlight.style.opacity = "0";
            card.style.left = "50%";
            card.style.top = "50%";
            card.style.width = "460px";
            card.style.transform = "translate(-50%, -50%)";
        } else {
            overlay.classList.add("with-backdrop");
            requestAnimationFrame(positionOnboardingSpotlight);
        }
    }

    function onboardingAction(action) {
        const steps = (S.onboarding && S.onboarding.steps) || [];
        if (!steps.length) return;
        if (action === "skip" || action === "done") { finishOnboarding(); return; }
        if (action === "prev") {
            S.onboarding.idx = Math.max(0, S.onboarding.idx - 1);
            renderOnboarding();
            return;
        }
        if (action === "next") {
            if (S.onboarding.idx >= steps.length - 1) {
                finishOnboarding();
            } else {
                S.onboarding.idx += 1;
                renderOnboarding();
            }
        }
    }


    // ------------------------------------------------------------------
    // Events
    // ------------------------------------------------------------------
    function bindEvents() {
        const bind = (id, handler) => { const node = $(id); if (node) node.addEventListener("click", handler); };
        bind("btn-toggle-power", togglePower);
        bind("btn-sync", doSync);
        bind("btn-open-webui", openWebUI);
        bind("btn-restart", doRestart);
        bind("btn-cleanup", doCleanup);
        bind("btn-refresh", doRefresh);
        bind("btn-clear-log", clearLogs);
        bind("btn-open-config", openConfig);
        bind("btn-config-cancel", closeConfig);
        bind("btn-config-save", saveConfig);
        bind("btn-scan", scanPaths);
        bind("btn-open-download", () => { closeConfig(); openDownload(); });
        bind("btn-install-deps", installLinuxDeps);
        bind("btn-replay-guide", () => { closeConfig(); startOnboarding(true); });
        bind("btn-dl-close", closeDownload);
        bind("btn-dl-install", doInstall);
        bind("btn-dl-uninstall", doUninstall);
        bind("btn-close-webui", () => { const modal = $("webui-modal"); if (modal) modal.classList.remove("visible"); });
        bind("btn-copy-webui-url", () => { const field = $("webui-url-field"); if (field) copyText(field.value || ""); });
        bind("btn-copy-webui-token", () => { const field = $("webui-token-field"); if (field) copyText(field.value || ""); });

        const themeToggle = $("cfg-light-mode");
        if (themeToggle) themeToggle.addEventListener("change", (event) => {
            const theme = event.target.checked ? "light" : "dark";
            S.themeTouched = true;
            applyTheme(theme, true);
            apiPost("ui/theme", { theme }).catch((error) => {
                toast("主题已应用，但保存失败：" + (error.message || error), "warn", 3000);
            });
        });
        const followTheme = $("link-follow-theme");
        if (followTheme) followTheme.addEventListener("click", (event) => {
            event.preventDefault();
            S.themeTouched = false;
            applyTheme("auto", true);
            apiPost("ui/theme", { theme: "auto" }).catch((error) => {
                toast("主题已应用，但保存失败：" + (error.message || error), "warn", 3000);
            });
        });

        const pause = $("log-pause");
        if (pause) pause.addEventListener("change", () => { S.paused = !!pause.checked; });

        const mirrorToggle = $("dl-mirror-toggle");
        if (mirrorToggle) mirrorToggle.addEventListener("click", (event) => {
            event.stopPropagation();
            const menu = $("dl-mirror-menu");
            setMirrorMenu(!(menu && menu.classList.contains("visible")));
        });
        const mirrorInput = $("dl-mirror-input");
        if (mirrorInput) {
            mirrorInput.addEventListener("input", () => { queueMirrorSave(); renderMirrorMenu(); });
            mirrorInput.addEventListener("click", (event) => event.stopPropagation());
        }
        const versionSelect = $("dl-version");
        if (versionSelect) versionSelect.addEventListener("change", () => {
            saveDownloadOptions({ napcat_version: versionSelect.value || "latest" }, false);
        });

        document.addEventListener("click", (event) => {
            if (!event.target.closest("#dl-mirror-picker")) setMirrorMenu(false);
        });
        document.addEventListener("click", (event) => {
            const ob = event.target.closest("[data-ob]");
            if (ob) onboardingAction(ob.dataset.ob);
        });
        document.addEventListener("keydown", (event) => {
            if (event.key !== "Escape") return;
            ["config-modal", "download-modal", "webui-modal", "sudo-modal"].forEach((id) => {
                const modal = $(id);
                if (modal) modal.classList.remove("visible");
            });
            if (idHidden("config-modal")) S.configOpen = false;
            if (idHidden("download-modal")) S.downloadOpen = false;
        });
        ["config-modal", "download-modal", "webui-modal", "sudo-modal"].forEach((id) => {
            const modal = $(id);
            if (!modal) return;
            modal.addEventListener("click", (event) => {
                if (event.target === modal && id !== "sudo-modal") {
                    modal.classList.remove("visible");
                    if (id === "config-modal") S.configOpen = false;
                    if (id === "download-modal") { S.downloadOpen = false; setMirrorMenu(false); }
                }
            });
        });
    }

    function idHidden(id) {
        const node = $(id);
        return !!node && !node.classList.contains("visible");
    }

    function applyContext(context) {
        S.context = context || {};
        if (S.context.pageTitle) document.title = S.context.pageTitle + " · NapCat Go";
        if (!S.themeTouched && S.themeMode === "auto") applyTheme("auto");
    }

    async function init() {
        S.bridge = await resolveBridge();
        bindEvents();
        try {
            const context = await Promise.race([S.bridge.ready(), sleep(4000).then(() => ({}))]);
            applyContext(context);
        } catch (error) {
            applyContext({});
        }
        if (S.bridge.onContext) {
            S.bridge.onContext((next) => applyContext(next));
        }

        // Show the guide quickly; never block it on a slow status API.
        let onboardingCfg = null;
        try {
            onboardingCfg = await Promise.race([apiGet("config"), sleep(1800)]);
        } catch (error) { /* ignore */ }
        if (onboardingCfg && onboardingCfg.onboarding_done
                && Number(onboardingCfg.onboarding_version || 0) >= ONBOARD_VERSION) {
            try { localStorage.setItem(ONBOARD_KEY, "1"); } catch (error) { /* ignore */ }
            finishOnboardingSilently();
        } else {
            startOnboarding(false);
        }

        refreshStatus();
        refreshLogs();
        S.statusTimer = setInterval(refreshStatus, STATUS_INTERVAL);
        S.logTimer = setInterval(refreshLogs, LOG_INTERVAL);
    }

    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", () => { init().catch((error) => console.error("[napcat] init failed", error)); });
    } else {
        init().catch((error) => console.error("[napcat] init failed", error));
    }
})();
