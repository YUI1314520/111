#!/usr/bin/env bash
# 服务器端部署脚本（在服务器上以普通用户身份运行）
# 用法：PANEL_PASSWORD='你的面板密码' ./deploy.sh
set -euo pipefail

PANEL_PASSWORD="${PANEL_PASSWORD:-}"
if [ -z "$PANEL_PASSWORD" ]; then
  echo "请先设置面板密码：PANEL_PASSWORD='...' $0" >&2
  exit 1
fi

echo "==> 1. systemd 服务"
sudo tee /etc/systemd/system/twitch-drops.service >/dev/null <<EOF
[Unit]
Description=Twitch Drops 自动化面板
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=ubuntu
Group=ubuntu
WorkingDirectory=/opt/twitch-drops
Environment=DROPS_HOST=127.0.0.1
Environment=DROPS_PORT=9200
Environment=DROPS_DATA_DIR=/var/lib/twitch-drops
Environment=DROPS_PASSWORD=${PANEL_PASSWORD}
Environment=PYTHONUNBUFFERED=1
ExecStart=/opt/twitch-drops/venv/bin/python -m app.main
Restart=always
RestartSec=5
MemoryMax=900M

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable twitch-drops >/dev/null 2>&1 || true
sudo systemctl restart twitch-drops

echo "==> 2. Caddy 路由"
sudo cp -n /etc/caddy/Caddyfile "/etc/caddy/Caddyfile.bak.$(date +%Y%m%d%H%M%S)" || true
if grep -q 'handle_path /drops/\*' /etc/caddy/Caddyfile; then
  echo "    已存在 /drops 路由，跳过"
else
  sudo python3 - <<'PY'
from pathlib import Path

path = Path('/etc/caddy/Caddyfile')
text = path.read_text(encoding='utf-8')
lines = text.splitlines()
block = [
    '\t# Twitch 掉宝面板（本机 9200，由 Caddy 反代）',
    '\tredir /drops /drops/',
    '\thandle_path /drops/* {',
    '\t\treverse_proxy 127.0.0.1:9200',
    '\t}',
]
# 插到最外层 :80 站点里那个兜底的 handle {} 之前
idx = None
for i in range(len(lines) - 1, -1, -1):
    if lines[i].strip() == 'handle {':
        idx = i
        break
if idx is None:
    raise SystemExit('没有找到兜底 handle 块，放弃自动修改')
lines[idx:idx] = block
path.write_text('\n'.join(lines) + '\n', encoding='utf-8')
print('    已插入 /drops 路由')
PY
  sudo caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile >/dev/null
  sudo systemctl reload caddy
  echo "    Caddy 已重载"
fi

echo "==> 3. 等待服务起来"
for i in $(seq 1 20); do
  if curl -fsS http://127.0.0.1:9200/api/health >/dev/null 2>&1; then
    break
  fi
  sleep 1
done

echo "==> 4. 自检"
echo -n "    本地 /api/health: "; curl -fsS http://127.0.0.1:9200/api/health || echo "失败"
echo
echo -n "    对外 /drops/api/health: "; curl -fsS http://127.0.0.1/drops/api/health || echo "失败"
echo
echo -n "    对外 /drops/ 页面: "; curl -fsS -o /dev/null -w '%{http_code}' http://127.0.0.1/drops/ || true
echo
echo -n "    /drops/style.css: "; curl -fsS -o /dev/null -w '%{http_code}' http://127.0.0.1/drops/style.css || true
echo
echo -n "    /drops/app.js: "; curl -fsS -o /dev/null -w '%{http_code}' http://127.0.0.1/drops/app.js || true
echo
echo "==> 完成"
