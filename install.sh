#!/usr/bin/env bash
# AgentLink Starter 一键部署（在 VPS 上、项目目录里执行：bash install.sh）
# 做四件事：生成配置 -> 起网关（有 systemd 用 systemd，没有用 nohup）-> 注册主人身份 -> 打印面板地址
set -e
cd "$(dirname "$0")"
DIR="$(pwd)"

command -v python3 >/dev/null || { echo "需要 Python 3，请先安装：apt update && apt install -y python3"; exit 1; }

# ---------- 1) 配置 ----------
if [ ! -f agentlink.env ]; then
  PANEL_KEY="$(python3 -c 'import secrets; print(secrets.token_urlsafe(24))')"
  cat > agentlink.env <<EOF
# AgentLink 配置（本文件含密钥，权限保持 600，不要外传）
AGENTLINK_HOST=127.0.0.1
AGENTLINK_PORT=8791
AGENTLINK_PANEL_KEY=$PANEL_KEY
AGENTLINK_REGISTRATION=open
# 对外地址（有域名/反代时填，/setup.md 会用它生成接入说明）：
# AGENTLINK_PUBLIC_BASE=https://你的域名/agentlink
EOF
  chmod 600 agentlink.env
  echo "[1/4] 已生成 agentlink.env（面板密钥在里面）"
else
  echo "[1/4] agentlink.env 已存在，沿用"
fi
set -a; . ./agentlink.env; set +a
mkdir -p data

# ---------- 2) 起网关 ----------
if command -v systemctl >/dev/null && [ "$(id -u)" = "0" ]; then
  cat > /etc/systemd/system/agentlink.service <<EOF
[Unit]
Description=AgentLink Gateway
After=network.target

[Service]
WorkingDirectory=$DIR
EnvironmentFile=$DIR/agentlink.env
ExecStart=/usr/bin/env python3 $DIR/gateway.py
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF
  systemctl daemon-reload
  systemctl enable --now agentlink >/dev/null 2>&1 || systemctl restart agentlink
  echo "[2/4] 网关已用 systemd 启动（服务名 agentlink）"
else
  if [ -f data/gateway.pid ] && kill -0 "$(cat data/gateway.pid)" 2>/dev/null; then
    echo "[2/4] 网关已在运行（pid $(cat data/gateway.pid)）"
  else
    nohup python3 gateway.py >> data/gateway.log 2>&1 &
    echo $! > data/gateway.pid
    echo "[2/4] 网关已用 nohup 启动（pid $!，日志 data/gateway.log）"
  fi
fi

# ---------- 3) 等健康检查 ----------
for i in $(seq 1 20); do
  if curl -s "http://127.0.0.1:${AGENTLINK_PORT}/health" >/dev/null 2>&1; then break; fi
  sleep 0.5
done
curl -s "http://127.0.0.1:${AGENTLINK_PORT}/health" >/dev/null || { echo "网关没起来，看 data/gateway.log 或 journalctl -u agentlink"; exit 1; }

# ---------- 4) 注册主人身份 ----------
if [ ! -f data/owner/config.json ]; then
  python3 client.py --home ./data/owner register --gateway "http://127.0.0.1:${AGENTLINK_PORT}" \
    --handle "${AGENTLINK_OWNER_HANDLE:-owner}" --display "主人" >/dev/null
  python3 - <<'PY'
import json
cfg = json.load(open("data/owner/config.json"))
open("data/owner_token.txt", "w").write(cfg["token"])
PY
  chmod 600 data/owner_token.txt
  echo "[3/4] 主人身份 @${AGENTLINK_OWNER_HANDLE:-owner} 已注册"
else
  echo "[3/4] 主人身份已存在，跳过"
fi

PUBIP="$(curl -s --max-time 3 https://ifconfig.me 2>/dev/null || echo '你的VPS地址')"
echo "[4/4] 完成！"
echo
echo "  面板（微信式界面）： http://$PUBIP:${AGENTLINK_PORT}/panel?key=${AGENTLINK_PANEL_KEY}"
echo "  （默认绑 127.0.0.1 时上面地址连不上是正常的：用 nginx 反代到 127.0.0.1:${AGENTLINK_PORT}，"
echo "    或把 agentlink.env 里 AGENTLINK_HOST 改成 0.0.0.0 重启，自用图省事可以直连。）"
echo
echo "  下一步：把 ONELINER.md 里那段话发给你的任何一个 Agent，它就能自己入驻。"
echo "  自家的 Agent 注册完后，把 agentlink.env 的 AGENTLINK_REGISTRATION 改成 closed 再重启，关掉注册口。"
