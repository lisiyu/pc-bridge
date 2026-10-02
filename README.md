# pc-bridge

一个小小的桥，让在云端运行的 AI 助手（Muse）能直接读写、编译和调试你本地电脑上的代码——云上决策、云下执行，全链路打通。

它只监听你电脑的 Tailscale 虚拟网卡，只有你白名单里的节点能连进来。桥窗口开着就能用，关掉窗口立即断开，控制权始终在你手里。

> 📖 完整的设计思路、安全模型和踩坑记录，看这篇：[《一个小小的桥：让 Muse 控制本地电脑的读写与调试，云上云下全链路打通》](./Tailscale-Python-bridge-guide.md)

## 两种实现

| 实现 | 文件 | 说明 |
|---|---|---|
| **Python 版（推荐）** | `pc-bridge.py` | 纯标准库，零第三方依赖，有 Python 3 就能跑，不触发杀软误报 |
| Go 版 | `main.go` | 功能等价的单文件实现，交叉编译一个 exe；无签名 exe 可能被 Windows Defender 启发式拦截 |

## 安全模型

| 层级 | 做法 |
|---|---|
| 网络层 | 只绑定 Tailscale 虚拟网卡 IP，绝不绑 `0.0.0.0`，公网扫不到 |
| 身份层 | 每个 `/v1/*` 请求用本机 `tailscale whois <来源IP>` 查节点名，白名单放行；可选 Bearer token 第二道鉴权 |
| 文件层 | 读白名单与写白名单分离（写含读）；`..` 穿越、符号链接用真实路径归一化后校验 |
| 命令层 | 命令前缀白名单 + 超时 + 输出上限（默认 2MB） |

⚠️ 写权限 + 命令执行 ≈ 完全控制。只把可信节点加进白名单，白名单目录能小就小；`python`、`go run` 这类前缀本身就是任意代码执行，放进来时心里要有数。

## 快速开始（Python 版）

前提：控制端和被控的 Windows 电脑装好 [Tailscale](https://tailscale.com/download) 并登录同一个网络；Windows 上装好 Python 3。

1. 把 `pc-bridge.py` 和 `run-bridge-py.bat` 放到同一目录（比如 `D:\pc-bridge\`）。
2. 用记事本打开 `run-bridge-py.bat`，改开头几行配置：

   ```bat
   set ALLOW_NODE=your-control-node   ← 控制端的 Tailscale 节点名
   set ALLOW_READ=%USERPROFILE%       ← 允许读的目录，逗号分隔
   set ALLOW_WRITE=                   ← 允许写的目录，留空 = 不给写
   set ALLOW_CMD=hdc                  ← 允许执行的命令前缀，逗号分隔
   ```

3. 双击 `run-bridge-py.bat`，黑窗口保持开着（关窗口 = 断桥）。
4. 控制端访问 `http://<被控机 Tailscale IP>:18743/health` 应返回 ok。

运行中还能在 `bridge>` 提示符里动态调整目录权限（立即生效、自动持久化）：

```
bridge> allow D:\data          加为只读
bridge> allow-write D:\work    加为可写
bridge> disallow D:\data       移除
bridge> dirs                   查看当前权限
bridge> quit                   停桥
```

Go 版用法类似：`go build -o pc-bridge.exe .` 后 `pc-bridge.exe --allow-node <节点名> --allow-dir <目录> --allow-cmd hdc`，或直接编辑 `run-bridge.bat` 双击运行。

## 接口一览

```
GET  /health                    存活探针，无需鉴权
GET  /v1/sysinfo                主机名、系统、Tailscale IP
GET  /v1/list?path=             列目录
GET  /v1/read?path=&tail_bytes= 读文件（tail_bytes 只取末尾 N 字节）
POST /v1/write                  写文件 {path, content, append, encoding: text|base64}
POST /v1/exec                   跑白名单命令 {cmd, timeout}
```

仓库里还带了个控制端小工具 `bridge_call.py`（通过环境变量 `PC_BRIDGE_HOST` 指定被控机 IP），方便在脚本里调用桥，例如 `bridge_call.py exec "git -C D:\work\project status --short"`。

## 注意事项

- `.bat` 启动脚本请保持纯 ASCII 和 CRLF 换行：中文 Windows 的 `cmd` 按 GBK 解析无 BOM 的 UTF-8 脚本会把内容吞坏（详见实践总结里的踩坑记录）。
- 桥无法看屏幕：它只能读写文件、返回命令输出文本。
- 换电脑/重装后 Tailscale IP 可能变化，记得同步更新控制端配置。
