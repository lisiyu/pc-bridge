// pc-bridge: 让用户的 Muse 助手通过 Tailscale 私有网络安全访问
// 用户 Windows 电脑上指定目录/命令的小型 HTTP 桥。
//
// 安全模型:
//  1. 只监听 Tailscale 网卡 IP(默认自动识别), 不监听 0.0.0.0, 局域网其他人连不进来;
//  2. 鉴权 = Tailscale 节点身份白名单: 每个请求用本机 `tailscale whois <来源IP>`
//     查出调用方节点名/ID, 必须在 --allow-node 里。无共享密钥可泄露;
//     可选再加 --token 做第二道 Bearer 鉴权;
//  3. 文件操作限制在 --allow-dir 之下(防 .. 穿越); 命令执行只允许 --allow-cmd
//     前缀白名单(如 hdc), 超时 + 输出截断。
//
// 用法示例(Windows, 管理员终端可选):
//   pc-bridge.exe --allow-node my-control-node --allow-dir C:\Users\you --allow-cmd hdc
package main

import (
	"bytes"
	"context"
	"crypto/subtle"
	"encoding/json"
	"flag"
	"fmt"
	"io"
	"net"
	"net/http"
	"os"
	"os/exec"
	"path/filepath"
	"regexp"
	"runtime"
	"strings"
	"sync"
	"time"
)

const version = "1.0.0"

type stringList []string

func (s *stringList) String() string { return strings.Join(*s, ",") }
func (s *stringList) Set(v string) error {
	// 支持逗号分隔一次写多个: --allow-cmd "hdc,ping"
	for _, part := range strings.Split(v, ",") {
		if p := strings.TrimSpace(part); p != "" {
			*s = append(*s, p)
		}
	}
	return nil
}

var (
	flagPort      = flag.Int("port", 18743, "监听端口")
	flagBind      = flag.String("bind", "auto", "监听地址: auto=自动识别 Tailscale 网卡 IP, 或显式 IP(如 127.0.0.1 本地测试)")
	flagAllowNode stringList
	flagToken     = flag.String("token", "", "可选的第二道鉴权: Bearer token(留空则只用节点白名单)")
	flagAllowDir  stringList
	flagAllowCmd  stringList
	flagExecTO    = flag.Int("exec-timeout", 60, "命令执行超时(秒)")
)

func init() {
	flag.Var(&flagAllowNode, "allow-node", "允许访问的 Tailscale 节点名或节点 ID(可重复)")
	flag.Var(&flagAllowDir, "allow-dir", "允许读取的目录白名单(可重复,默认 %USERPROFILE%)")
	flag.Var(&flagAllowCmd, "allow-cmd", "允许执行的命令前缀白名单(可重复,默认 hdc)")
}

// ---------- Tailscale 网卡识别 ----------

// tailscaleIPv4 返回本机 Tailscale 网卡的第一个 IPv4 地址。
func tailscaleIPv4() (string, error) {
	ifaces, err := net.Interfaces()
	if err != nil {
		return "", err
	}
	for _, iface := range ifaces {
		if !strings.Contains(strings.ToLower(iface.Name), "tailscale") {
			continue
		}
		addrs, err := iface.Addrs()
		if err != nil {
			continue
		}
		for _, a := range addrs {
			ipnet, ok := a.(*net.IPNet)
			if !ok {
				continue
			}
			ip := ipnet.IP.To4()
			if ip == nil || ip.IsLoopback() {
				continue
			}
			return ip.String(), nil
		}
	}
	return "", fmt.Errorf("Tailscale adapter not found (is Tailscale installed and logged in?)")
}

// ---------- 节点身份鉴权 ----------

type whoisResult struct {
	nodeName string
	nodeID   string
	ok       bool
}

var (
	whoisCache   = map[string]cachedWhois{}
	whoisCacheMu sync.Mutex
)

type cachedWhois struct {
	res     whoisResult
	expires time.Time
}

var nodeIDRe = regexp.MustCompile(`\bn[A-Za-z0-9]{10,}\b`)

// tailscaleWhois 用本机 tailscale CLI 查来源 IP 的节点身份, 结果缓存 5 分钟。
func tailscaleWhois(ip string) whoisResult {
	whoisCacheMu.Lock()
	if c, ok := whoisCache[ip]; ok && time.Now().Before(c.expires) {
		whoisCacheMu.Unlock()
		return c.res
	}
	whoisCacheMu.Unlock()

	res := whoisResult{}
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	out, err := exec.CommandContext(ctx, "tailscale", "whois", ip).CombinedOutput()
	if err == nil {
		text := string(out)
		for _, line := range strings.Split(text, "\n") {
			line = strings.TrimSpace(line)
			// 形如 "Name:   foo.example.com" / "ID:     nABC..."
			if strings.HasPrefix(strings.ToLower(line), "name:") && res.nodeName == "" {
				res.nodeName = strings.TrimSpace(line[len("name:"):])
			}
			if strings.HasPrefix(strings.ToLower(line), "id:") && res.nodeID == "" {
				res.nodeID = strings.TrimSpace(line[len("id:"):])
			}
		}
		if res.nodeID == "" {
			if m := nodeIDRe.FindString(text); m != "" {
				res.nodeID = m
			}
		}
		if res.nodeName != "" || res.nodeID != "" {
			res.ok = true
		}
	}
	whoisCacheMu.Lock()
	whoisCache[ip] = cachedWhois{res: res, expires: time.Now().Add(5 * time.Minute)}
	whoisCacheMu.Unlock()
	return res
}

// nodeAllowed 检查来源 IP 的节点是否在白名单(名或 ID, 名支持短名匹配 foo == foo.example.com)。
func nodeAllowed(ip string) (string, bool) {
	if len(flagAllowNode) == 0 {
		return "", false
	}
	w := tailscaleWhois(ip)
	if !w.ok {
		return "", false
	}
	name := strings.ToLower(w.nodeName)
	short := name
	if i := strings.Index(short, "."); i > 0 {
		short = short[:i]
	}
	for _, allow := range flagAllowNode {
		a := strings.ToLower(strings.TrimSpace(allow))
		if a == "" {
			continue
		}
		if a == name || a == short || strings.EqualFold(w.nodeID, allow) {
			return w.nodeName, true
		}
	}
	return w.nodeName, false
}

func authed(r *http.Request) (string, bool) {
	host, _, err := net.SplitHostPort(r.RemoteAddr)
	if err != nil {
		host = r.RemoteAddr
	}
	if id, ok := nodeAllowed(host); ok {
		return "node:" + id, true
	}
	if *flagToken != "" {
		got := strings.TrimPrefix(r.Header.Get("Authorization"), "Bearer ")
		if subtle.ConstantTimeCompare([]byte(got), []byte(*flagToken)) == 1 && got != "" {
			return "token", true
		}
	}
	return "", false
}

func requireAuth(next http.HandlerFunc) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		who, ok := authed(r)
		start := time.Now()
		if !ok {
			writeJSON(w, http.StatusForbidden, map[string]string{"error": "unauthorized: caller Tailscale node not in allowlist"})
			fmt.Printf("%s %s %s -> 403 unauthorized (%v)\n",
				time.Now().Format("15:04:05"), r.Method, r.URL.Path, time.Since(start).Round(time.Millisecond))
			return
		}
		next(w, r)
		fmt.Printf("%s %s %s by %s (%v)\n",
			time.Now().Format("15:04:05"), r.Method, r.URL.Path, who, time.Since(start).Round(time.Millisecond))
	}
}

// ---------- 路径沙箱 ----------

var allowDirsAbs []string

func initAllowDirs() error {
	dirs := flagAllowDir
	if len(dirs) == 0 {
		home := os.Getenv("USERPROFILE")
		if home == "" {
			home = os.Getenv("HOME")
		}
		if home == "" {
			return fmt.Errorf("cannot determine home dir, please set --allow-dir explicitly")
		}
		dirs = []string{home}
	}
	for _, d := range dirs {
		abs, err := filepath.Abs(d)
		if err != nil {
			return fmt.Errorf("invalid --allow-dir %q: %v", d, err)
		}
		allowDirsAbs = append(allowDirsAbs, abs)
	}
	return nil
}

// sandboxAbs 把用户传入路径解析为绝对路径, 并确保落在白名单目录内。
func sandboxAbs(p string) (string, error) {
	if p == "" {
		return "", fmt.Errorf("缺少 path 参数")
	}
	abs, err := filepath.Abs(filepath.Clean(p))
	if err != nil {
		return "", err
	}
	for _, dir := range allowDirsAbs {
		if abs == dir || strings.HasPrefix(strings.ToLower(abs), strings.ToLower(dir+string(filepath.Separator))) {
			return abs, nil
		}
	}
	return "", fmt.Errorf("路径超出白名单目录: %s", p)
}

// ---------- 接口 ----------

func writeJSON(w http.ResponseWriter, code int, v any) {
	w.Header().Set("Content-Type", "application/json; charset=utf-8")
	w.WriteHeader(code)
	_ = json.NewEncoder(w).Encode(v)
}

func handleHealth(w http.ResponseWriter, r *http.Request) {
	hn, _ := os.Hostname()
	writeJSON(w, http.StatusOK, map[string]any{
		"ok": true, "service": "pc-bridge", "version": version, "hostname": hn,
	})
}

func handleSysInfo(w http.ResponseWriter, r *http.Request) {
	hn, _ := os.Hostname()
	ips := []string{}
	if ts, err := tailscaleIPv4(); err == nil {
		ips = append(ips, ts)
	}
	writeJSON(w, http.StatusOK, map[string]any{
		"hostname": hn, "os": runtime.GOOS, "arch": runtime.GOARCH,
		"tailscale_ip": ips, "time": time.Now().Format(time.RFC3339),
	})
}

func handleList(w http.ResponseWriter, r *http.Request) {
	abs, err := sandboxAbs(r.URL.Query().Get("path"))
	if err != nil {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": err.Error()})
		return
	}
	entries, err := os.ReadDir(abs)
	if err != nil {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": err.Error()})
		return
	}
	type ent struct {
		Name    string `json:"name"`
		IsDir   bool   `json:"is_dir"`
		Size    int64  `json:"size"`
		ModTime string `json:"mod_time"`
	}
	out := []ent{}
	for _, e := range entries {
		fi, err := e.Info()
		if err != nil {
			continue
		}
		out = append(out, ent{e.Name(), e.IsDir(), fi.Size(), fi.ModTime().Format(time.RFC3339)})
	}
	writeJSON(w, http.StatusOK, map[string]any{"path": abs, "entries": out})
}

const maxReadBytes = 2 << 20 // 2MB

func handleRead(w http.ResponseWriter, r *http.Request) {
	abs, err := sandboxAbs(r.URL.Query().Get("path"))
	if err != nil {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": err.Error()})
		return
	}
	fi, err := os.Stat(abs)
	if err != nil {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": err.Error()})
		return
	}
	if fi.IsDir() {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "是目录, 请用 /v1/list"})
		return
	}
	var tailBytes int64
	if t := r.URL.Query().Get("tail_bytes"); t != "" {
		_, _ = fmt.Sscanf(t, "%d", &tailBytes)
		if tailBytes < 0 {
			tailBytes = 0
		}
		if tailBytes > maxReadBytes {
			tailBytes = maxReadBytes
		}
	}
	f, err := os.Open(abs)
	if err != nil {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": err.Error()})
		return
	}
	defer f.Close()

	var (
		data      []byte
		truncated bool
	)
	if tailBytes > 0 && fi.Size() > tailBytes {
		// 只读最后 tailBytes 字节
		_, err = f.Seek(-tailBytes, io.SeekEnd)
		if err != nil {
			writeJSON(w, http.StatusInternalServerError, map[string]string{"error": err.Error()})
			return
		}
		data = make([]byte, tailBytes)
		_, err = io.ReadFull(f, data)
		truncated = true
	} else {
		// 读全文, 上限 2MB
		lr := io.LimitReader(f, maxReadBytes+1)
		data, err = io.ReadAll(lr)
		if int64(len(data)) > maxReadBytes {
			data = data[:maxReadBytes]
			truncated = true
		}
	}
	if err != nil && err != io.EOF && err != io.ErrUnexpectedEOF {
		writeJSON(w, http.StatusInternalServerError, map[string]string{"error": err.Error()})
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{
		"path": abs, "size": fi.Size(), "truncated": truncated,
		"content": string(data),
	})
}

type execReq struct {
	Cmd      string `json:"cmd"`
	TimeoutS int    `json:"timeout_s"`
}

func cmdAllowed(cmd string) bool {
	c := strings.TrimSpace(cmd)
	for _, p := range flagAllowCmd {
		p = strings.TrimSpace(p)
		if p == "" {
			continue
		}
		if c == p || strings.HasPrefix(c, p+" ") {
			return true
		}
	}
	return false
}

func handleExec(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		writeJSON(w, http.StatusMethodNotAllowed, map[string]string{"error": "请用 POST"})
		return
	}
	var req execReq
	if err := json.NewDecoder(http.MaxBytesReader(w, r.Body, 1<<20)).Decode(&req); err != nil {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "请求体应为 JSON {cmd, timeout_s?}: " + err.Error()})
		return
	}
	if strings.TrimSpace(req.Cmd) == "" {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "cmd 为空"})
		return
	}
	if !cmdAllowed(req.Cmd) {
		writeJSON(w, http.StatusForbidden, map[string]string{"error": "命令不在白名单(--allow-cmd)内"})
		return
	}
	to := req.TimeoutS
	if to <= 0 || to > 300 {
		to = *flagExecTO
	}
	ctx, cancel := context.WithTimeout(context.Background(), time.Duration(to)*time.Second)
	defer cancel()

	var c *exec.Cmd
	if runtime.GOOS == "windows" {
		c = exec.CommandContext(ctx, "cmd", "/C", req.Cmd)
	} else {
		c = exec.CommandContext(ctx, "sh", "-c", req.Cmd)
	}
	var stdout, stderr bytes.Buffer
	// 输出上限 2MB, 防止刷屏
	c.Stdout = &limitedWriter{W: &stdout, N: maxReadBytes}
	c.Stderr = &limitedWriter{W: &stderr, N: maxReadBytes}
	err := c.Run()
	exitCode := 0
	timedOut := ctx.Err() == context.DeadlineExceeded
	if err != nil {
		if ee, ok := err.(*exec.ExitError); ok {
			exitCode = ee.ExitCode()
		} else {
			exitCode = -1
		}
	}
	writeJSON(w, http.StatusOK, map[string]any{
		"cmd": req.Cmd, "exit_code": exitCode, "timed_out": timedOut,
		"stdout": stdout.String(), "stderr": stderr.String(),
	})
}

type limitedWriter struct {
	W io.Writer
	N int64
}

func (l *limitedWriter) Write(p []byte) (int, error) {
	if l.N <= 0 {
		return len(p), nil // 丢弃超量部分
	}
	if int64(len(p)) > l.N {
		p = p[:l.N]
	}
	n, err := l.W.Write(p)
	l.N -= int64(n)
	return len(p), err
}

// fail 打印错误并退出。在 Windows 下双击运行时会等待按 Enter,
// 避免控制台一闪而过看不到报错。
func fail(code int, format string, args ...any) {
	fmt.Fprintf(os.Stderr, "pc-bridge failed to start: "+format+"\n", args...)
	if runtime.GOOS == "windows" {
		fmt.Fprintln(os.Stderr, "\nPress Enter to close this window...")
		var b [1]byte
		_, _ = os.Stdin.Read(b[:])
	}
	os.Exit(code)
}

func main() {
	flag.Parse()
	if len(flagAllowNode) == 0 && *flagToken == "" {
		fail(2, "must set --allow-node or --token (auth cannot be empty)")
	}
	if len(flagAllowCmd) == 0 {
		flagAllowCmd = stringList{"hdc"}
	}
	if err := initAllowDirs(); err != nil {
		fail(2, "failed to init allow-dir: %v", err)
	}
	bindIP := *flagBind
	if bindIP == "auto" {
		ip, err := tailscaleIPv4()
		if err != nil {
			fail(2, "%v\nfor local testing use --bind 127.0.0.1", err)
		}
		bindIP = ip
	}
	addr := fmt.Sprintf("%s:%d", bindIP, *flagPort)

	mux := http.NewServeMux()
	mux.HandleFunc("/health", handleHealth) // 存活探针, 无需鉴权
	mux.HandleFunc("/v1/sysinfo", requireAuth(handleSysInfo))
	mux.HandleFunc("/v1/list", requireAuth(handleList))
	mux.HandleFunc("/v1/read", requireAuth(handleRead))
	mux.HandleFunc("/v1/exec", requireAuth(handleExec))

	hn, _ := os.Hostname()
	fmt.Printf("pc-bridge v%s started\n", version)
	fmt.Printf("  listening: http://%s (Tailscale interface only)\n", addr)
	fmt.Printf("  hostname: %s\n", hn)
	fmt.Printf("  allowed nodes: %s\n", strings.Join(flagAllowNode, ", "))
	if *flagToken != "" {
		fmt.Printf("  extra Bearer token auth: enabled\n")
	}
	fmt.Printf("  allowed dirs: %s\n", strings.Join(allowDirsAbs, "; "))
	fmt.Printf("  allowed cmd prefixes: %s\n", strings.Join(flagAllowCmd, ", "))
	fmt.Println("Close this window to stop the bridge.")

	srv := &http.Server{
		Addr:              addr,
		Handler:           mux,
		ReadHeaderTimeout: 10 * time.Second,
	}
	if err := srv.ListenAndServe(); err != nil && err != http.ErrServerClosed {
		fail(1, "listen failed: %v", err)
	}
}
