#!/usr/bin/env bash
# wifi-gate 运维入口：在 NAS 上和 wifi-gate 目录同级使用。
#   ./gate.sh up        构建并常驻启动
#   ./gate.sh status    看状态与健康检查
#   ./gate.sh logs      跟随日志
#   ./gate.sh selftest  在容器内跑全链路自检
#   ./gate.sh probe ... 在容器内跑只读探测（参数原样透传）
#   ./gate.sh restore   应急一键放行所有记录过的设备
#   ./gate.sh check     部署前配置检查
#   ./gate.sh restart | down
set -euo pipefail

cd "$(dirname "$0")"

if docker compose version >/dev/null 2>&1; then
  DC="docker compose"
elif command -v docker-compose >/dev/null 2>&1; then
  DC="docker-compose"
else
  echo "未找到 docker compose，请先安装 Docker 与 Compose 插件。" >&2
  exit 1
fi

PORT=$(grep -o '"port"[[:space:]]*:[[:space:]]*[0-9]*' config.json 2>/dev/null \
       | head -1 | grep -o '[0-9]*$' || true)
PORT=${PORT:-8080}

lan_ip() {
  local ip=""
  ip=$(hostname -I 2>/dev/null | awk '{print $1}')
  [ -z "$ip" ] && ip=$(ip -4 -o addr show scope global 2>/dev/null \
       | awk '{split($4,a,"/"); print a[1]}' | grep -v '^172\.1[6-9]\.\|^172\.2[0-9]\.\|^172\.3[01]\.\|^10\.' | head -1)
  if [ -z "$ip" ]; then
    for i in en0 en1 en2 en3 en4 en5; do
      ip=$(ipconfig getifaddr "$i" 2>/dev/null) && [ -n "$ip" ] && break
    done
  fi
  echo "${ip:-<NAS内网IP>}"
}

set_dry_run() {
  local want="$1"
  if command -v python3 >/dev/null 2>&1; then
    python3 - "$want" <<'PY'
import json, sys
want = sys.argv[1] == "true"
p = "config.json"
c = json.load(open(p, encoding="utf-8"))
c.setdefault("router", {})["dry_run"] = want
c["router"]["mode"] = "miwifi"
with open(p, "w", encoding="utf-8") as f:
    json.dump(c, f, ensure_ascii=False, indent=2)
print("router.mode = miwifi, router.dry_run =", want)
PY
  elif command -v perl >/dev/null 2>&1; then
    perl -0pi -e 's/("dry_run"\s*:\s*)(true|false)/${1}'"$want"'/' config.json
    echo "router.dry_run = $want"
  else
    echo "请手动编辑 config.json 里的 \"dry_run\": $want" >&2
    return 1
  fi
}

CMD=${1:-up}
shift || true

case "$CMD" in
  up)
    if [ ! -f config.json ]; then
      cp config.example.json config.json
      echo "已从 config.example.json 生成 config.json（默认 mock 模式，不碰路由器）"
    fi
    mkdir -p state
    chmod 600 config.json 2>/dev/null || true
    echo "构建并启动 wifi-gate（host 网络，端口 $PORT）..."
    $DC up -d --build
    echo
    echo "答题页： http://$(lan_ip):$PORT/"
    echo "管理后台： http://$(lan_ip):$PORT/admin"
    echo "初始管理密码见 config.json 的 server.admin_password"
    echo "提示：请用手机访问 http://$(lan_ip):$PORT/api/quiz，"
    echo "      返回的 ip 字段必须是手机自己的内网 IP，不能是 172.17.x.x。"
    $DC ps
    ;;
  down)
    $DC down
    ;;
  restart)
    $DC restart
    ;;
  status)
    $DC ps
    echo "--- health ---"
    curl -s --max-time 5 "http://127.0.0.1:$PORT/api/health" || echo "健康检查失败"
    echo
    ;;
  logs)
    $DC logs -f --tail "${1:-100}"
    ;;
  selftest)
    $DC exec -T wifi-gate python3 /app/selftest.py
    ;;
  probe)
    $DC run --rm --no-deps --entrypoint python3 wifi-gate \
      /app/probe_miwifi.py "$@"
    ;;
  sim)
    # 模拟手机走一遍答题流程：./gate.sh sim --mode correct --password 管理密码
    $DC exec -T wifi-gate python3 /app/simulate_device.py \
      --url "http://127.0.0.1:$PORT" "$@"
    ;;
  dryrun)
    set_dry_run true
    $DC up -d >/dev/null
    echo "已切到演练模式：会真实登录路由器、解析 MAC，但不修改任何规则。"
    ;;
  live)
    set_dry_run false
    $DC up -d >/dev/null
    echo "已关闭演练：答对后会真正调用路由器放行接口。"
    echo "如果还没做过「暂停联网后仍能打开答题页」的实测，请先 ./gate.sh dryrun"
    ;;
  restore)
    echo "应急放行：把 state/grants.json 里记录过的设备全部重新允许上网"
    $DC exec -T wifi-gate python3 /app/server.py --restore-all
    ;;
  check)
    if command -v python3 >/dev/null 2>&1; then
      python3 docker-entrypoint.py --check
    else
      $DC run --rm --no-deps --entrypoint python3 wifi-gate \
        /app/docker-entrypoint.py --check
    fi
    ;;
  *)
    echo "用法: ./gate.sh {up|down|restart|status|logs|selftest|probe|sim|dryrun|live|restore|check}"
    exit 1
    ;;
esac
