#!/bin/sh
# update-allow-cmd.sh - 一键更新 pc-bridge 命令白名单（systemd 方式）。
#
# 适用：桥是用 install-ubuntu.sh 装的 systemd 服务（/etc/systemd/system/pc-bridge.service）。
# 只改 --allow-cmd 这一项，其他配置（节点、读写目录）原样不动。
#
# 用法（在桥所在机器上跑）：
#   curl -fsSL https://raw.githubusercontent.com/lisiyu/pc-bridge/main/update-allow-cmd.sh | sudo bash
#
# 自定义命令列表（先下载再跑，管道进 sudo bash 会丢环境变量）：
#   curl -fsSL https://raw.githubusercontent.com/lisiyu/pc-bridge/main/update-allow-cmd.sh -o update-allow-cmd.sh
#   ALLOW_CMD="git,echo" sudo bash update-allow-cmd.sh
#
# 环境变量：
#   ALLOW_CMD     新的白名单，逗号分隔（默认见下）
#   SERVICE_FILE  service 文件路径（默认 /etc/systemd/system/pc-bridge.service）
#
# 注意：sh、python 一旦放行，白名单基本形同虚设（sh -c 后面跟什么都能跑），
# 等于完全信任控制端和这条 tailnet；且白名单是"开头匹配"，放了 sh 就等于
# 放了 shutdown、shred，放了 python 就等于放了 python3。

set -eu

ALLOW_CMD="${ALLOW_CMD:-hdc,echo,git,go,python,java,javac,ls,cat,uname,systemctl,tailscale,sh,curl,apt-get}"
SERVICE_FILE="${SERVICE_FILE:-/etc/systemd/system/pc-bridge.service}"

[ "$(id -u)" -eq 0 ] || {
    echo "请用 sudo 运行：curl -fsSL https://raw.githubusercontent.com/lisiyu/pc-bridge/main/update-allow-cmd.sh | sudo bash" >&2
    exit 1
}
[ -f "$SERVICE_FILE" ] || {
    echo "找不到 $SERVICE_FILE：这台机器的桥可能不是 systemd 方式装的。" >&2
    echo "前台 run-bridge.sh 方式请改 ALLOW_CMD 后重启，见 README。" >&2
    exit 1
}
grep -q -- "--allow-cmd" "$SERVICE_FILE" || {
    echo "$SERVICE_FILE 里没有 --allow-cmd，无法更新。" >&2
    exit 1
}

echo "==> 备份原文件到 $SERVICE_FILE.bak"
cp "$SERVICE_FILE" "$SERVICE_FILE.bak"

# sed 替换值里的 & \ | 要先转义
esc() { printf '%s' "$1" | sed -e 's/[\\&|]/\\&/g'; }
ALLOW_CMD_E=$(esc "$ALLOW_CMD")
sed -i "s|--allow-cmd [^ ]*|--allow-cmd $ALLOW_CMD_E|" "$SERVICE_FILE"

echo "==> 更新后的 ExecStart："
grep "^ExecStart=" "$SERVICE_FILE" || true

echo "==> 重启服务 ..."
systemctl daemon-reload
systemctl restart pc-bridge
sleep 2
systemctl is-active --quiet pc-bridge || {
    echo "服务启动失败，日志如下：" >&2
    systemctl status pc-bridge --no-pager || true
    echo "原文件已备份在 $SERVICE_FILE.bak，可手动恢复。" >&2
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
        echo "==> 警告：服务已启动，但健康检查未通过，请看日志：journalctl -u pc-bridge -f" >&2
    fi
fi

echo
echo "完成。当前命令白名单：$ALLOW_CMD"
echo "可以让控制端测一下 echo / git 是不是还报 403，不报就是放开了。"
