#!/bin/sh
# install-ubuntu.sh - pc-bridge 一键安装脚本（Ubuntu / Debian）。
#
# 一条命令装好依赖、拉取代码、装好 systemd 服务并开机自启：
#
#   ALLOW_NODE=<控制端节点名> bash -c "$(curl -fsSL https://raw.githubusercontent.com/lisiyu/pc-bridge/main/install-ubuntu.sh)"
#
# 常用环境变量（不设就用默认值，ALLOW_NODE 未设时会交互询问）：
#   ALLOW_NODE    控制端的 Tailscale 节点名（必填，没有默认值）
#   ALLOW_READ    允许读的目录，逗号分隔（默认：运行用户的家目录）
#   ALLOW_WRITE   允许写的目录，逗号分隔（默认空 = 不给写）
#   ALLOW_CMD     允许执行的命令前缀，逗号分隔（默认 hdc）
#   INSTALL_DIR   代码装到哪里（默认 /opt/pc-bridge）
#   SERVICE_USER  systemd 服务以哪个用户运行（默认：当前用户）
#   REPO_URL      代码仓库（默认本仓库，测试时可指向本地路径）
#   BRANCH        分支（默认 main）
#
# 跳过某些步骤（一般用不上，测试/排障用）：
#   SKIP_DEPS=1       不跑 apt 安装依赖
#   SKIP_TAILSCALE=1  不装、不登录 Tailscale
#   SKIP_SYSTEMD=1    只生成 service 文件，不 enable、不启动
#   SYSTEMD_DIR       service 文件写到哪里（默认 /etc/systemd/system）
#
# 装完可用命令：
#   systemctl status pc-bridge     看状态
#   journalctl -u pc-bridge -f     看日志
#   systemctl restart pc-bridge    改配置后重启
#   systemctl stop pc-bridge       停桥
# 卸载：
#   sudo systemctl disable --now pc-bridge
#   sudo rm /etc/systemd/system/pc-bridge.service
#   sudo systemctl daemon-reload
#   sudo rm -rf /opt/pc-bridge

set -eu

if [ "${1:-}" = "-h" ] || [ "${1:-}" = "--help" ]; then
    sed -n '2,/^$/p' "$0" | sed 's/^# \?//'
    exit 0
fi

# ---- 配置 ----
ALLOW_NODE="${ALLOW_NODE:-}"
ALLOW_WRITE="${ALLOW_WRITE:-}"
ALLOW_CMD="${ALLOW_CMD:-hdc}"
INSTALL_DIR="${INSTALL_DIR:-/opt/pc-bridge}"
REPO_URL="${REPO_URL:-https://github.com/lisiyu/pc-bridge.git}"
BRANCH="${BRANCH:-main}"
SYSTEMD_DIR="${SYSTEMD_DIR:-/etc/systemd/system}"
SERVICE_USER="${SERVICE_USER:-${SUDO_USER:-$(id -un)}}"
# --------------

# 需要提权时用 sudo；已是 root 则不用
if [ "$(id -u)" -eq 0 ]; then
    SUDO=""
elif command -v sudo >/dev/null 2>&1; then
    SUDO="sudo"
else
    echo "pc-bridge: 需要 root 或 sudo 权限才能安装。" >&2
    exit 1
fi

# 控制端节点名：没给就交互问一句（curl 管道进 bash 时也能从终端读）
if [ -z "$ALLOW_NODE" ]; then
    if [ -r /dev/tty ]; then
        printf '控制端 Tailscale 节点名 (ALLOW_NODE): ' > /dev/tty
        read -r ALLOW_NODE < /dev/tty
    fi
    if [ -z "$ALLOW_NODE" ]; then
        echo "pc-bridge: 请设置 ALLOW_NODE=控制端节点名 后再运行。" >&2
        exit 1
    fi
fi

# 读目录默认取服务用户的家目录
USER_HOME=$(getent passwd "$SERVICE_USER" | cut -d: -f6 || true)
[ -n "$USER_HOME" ] || USER_HOME="/home/$SERVICE_USER"
ALLOW_READ="${ALLOW_READ:-$USER_HOME}"

echo "pc-bridge 安装配置："
echo "  安装目录 : $INSTALL_DIR"
echo "  服务用户 : $SERVICE_USER"
echo "  控制节点 : $ALLOW_NODE"
echo "  允许读   : $ALLOW_READ"
echo "  允许写   : ${ALLOW_WRITE:-(none)}"
echo "  允许命令 : $ALLOW_CMD"
echo

# ---- 1. 系统依赖 ----
if [ -z "${SKIP_DEPS:-}" ]; then
    command -v apt-get >/dev/null 2>&1 || {
        echo "pc-bridge: 只支持 Ubuntu/Debian（需要 apt-get）。" >&2
        exit 1
    }
    echo "==> 安装依赖 python3 / git / curl ..."
    $SUDO apt-get update
    $SUDO apt-get install -y python3 git curl ca-certificates
fi

# ---- 2. Tailscale ----
if [ -z "${SKIP_TAILSCALE:-}" ]; then
    if ! command -v tailscale >/dev/null 2>&1; then
        echo "==> 安装 Tailscale ..."
        curl -fsSL https://tailscale.com/install.sh | $SUDO sh
    fi
    if ! tailscale ip -4 >/dev/null 2>&1; then
        echo "==> 登录 Tailscale（会显示一个登录链接，在浏览器里打开完成授权）..."
        $SUDO tailscale up
    fi
    tailscale ip -4 >/dev/null 2>&1 || {
        echo "pc-bridge: Tailscale 仍未登录，请先跑 'sudo tailscale up' 再重装。" >&2
        exit 1
    }
    echo "==> Tailscale 已就绪：$(tailscale ip -4)"
fi

# ---- 3. 拉取代码 ----
if [ -d "$INSTALL_DIR/.git" ]; then
    echo "==> 代码已存在，更新到最新 ..."
    if ! $SUDO git -C "$INSTALL_DIR" pull --ff-only; then
        echo "pc-bridge: $INSTALL_DIR 里的代码与上游分叉（可能本地改过），无法自动更新。" >&2
        echo "  想丢弃本地改动重装：sudo rm -rf $INSTALL_DIR 后重跑本脚本。" >&2
        exit 1
    fi
else
    echo "==> 拉取代码到 $INSTALL_DIR ..."
    $SUDO git clone --depth 1 --branch "$BRANCH" "$REPO_URL" "$INSTALL_DIR"
fi
$SUDO chown -R "$SERVICE_USER" "$INSTALL_DIR"

# ---- 4. 生成 systemd 服务 ----
SERVICE_SRC="$INSTALL_DIR/pc-bridge.service"
[ -f "$SERVICE_SRC" ] || {
    echo "pc-bridge: 仓库里缺少 pc-bridge.service，安装中止。" >&2
    exit 1
}
# sed 替换值里的 & \ | 要先转义，否则会被 sed 当特殊字符
esc() { printf '%s' "$1" | sed -e 's/[\\&|]/\\&/g'; }
ALLOW_READ_E=$(esc "$ALLOW_READ")
ALLOW_CMD_E=$(esc "$ALLOW_CMD")
SERVICE_USER_E=$(esc "$SERVICE_USER")
ALLOW_NODE_E=$(esc "$ALLOW_NODE")
INSTALL_DIR_E=$(esc "$INSTALL_DIR")
if [ -n "$ALLOW_WRITE" ]; then
    ALLOW_WRITE_E=$(esc "$ALLOW_WRITE")
    WRITE_SED="s|--allow-write-dir [^ ]*|--allow-write-dir $ALLOW_WRITE_E|"
else
    WRITE_SED="s| --allow-write-dir [^ ]*||"
fi
echo "==> 生成 systemd 服务 ..."
sed -e "s|your-username|$SERVICE_USER_E|g" \
    -e "s|your-control-node|$ALLOW_NODE_E|g" \
    -e "s|/opt/pc-bridge|$INSTALL_DIR_E|g" \
    -e "s|--allow-dir [^ ]*|--allow-dir $ALLOW_READ_E|" \
    -e "$WRITE_SED" \
    -e "s|--allow-cmd [^ ]*|--allow-cmd $ALLOW_CMD_E|" \
    "$SERVICE_SRC" | $SUDO tee "$SYSTEMD_DIR/pc-bridge.service" > /dev/null

# ---- 5. 启动并设开机自启 ----
if [ -z "${SKIP_SYSTEMD:-}" ]; then
    echo "==> 启动服务并设开机自启 ..."
    $SUDO systemctl daemon-reload
    $SUDO systemctl enable --now pc-bridge
    sleep 2
    $SUDO systemctl is-active --quiet pc-bridge || {
        echo "pc-bridge: 服务启动失败，日志如下：" >&2
        $SUDO systemctl status pc-bridge --no-pager || true
        exit 1
    }
    # 健康检查：桥只绑 Tailscale 网卡，用本机 Tailscale IP 访问
    if TS_IP=$(tailscale ip -4 2>/dev/null) && [ -n "$TS_IP" ]; then
        OK=""
        i=0
        while [ "$i" -lt 5 ]; do
            if curl -fsS --max-time 10 "http://$TS_IP:18743/health" >/dev/null 2>&1; then
                OK=1
                break
            fi
            i=$((i + 1))
            sleep 2
        done
        if [ -n "$OK" ]; then
            echo "==> 健康检查通过：http://$TS_IP:18743/health"
        else
            echo "pc-bridge: 服务已启动，但健康检查未通过，请看日志：journalctl -u pc-bridge -f" >&2
            exit 1
        fi
    fi
else
    echo "==> SKIP_SYSTEMD：service 文件已生成，未启用、未启动。"
fi

echo
if [ -z "${SKIP_SYSTEMD:-}" ]; then
    echo "安装完成。桥已在后台运行，开机自启。"
    echo "  状态：systemctl status pc-bridge"
    echo "  日志：journalctl -u pc-bridge -f"
    echo "  停止：systemctl stop pc-bridge"
else
    echo "完成（SKIP_SYSTEMD）：service 文件已生成在 $SYSTEMD_DIR/pc-bridge.service，未启用、未启动。"
    echo "  手动启用：sudo cp $SYSTEMD_DIR/pc-bridge.service /etc/systemd/system/ && sudo systemctl daemon-reload && sudo systemctl enable --now pc-bridge"
fi
