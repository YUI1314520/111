# Twitch 掉宝自动化面板（twitch-drops）

自动监控 Twitch 掉宝（Drops）活动，自动挑频道观看推进进度，达标后自动领取奖励。
纯协议实现（无 headless 浏览器），内存占用低，可同时运行多个账号，Web 面板手机/电脑都能看。

## 功能

- **多账号**：每个账号一个独立后台任务，互不影响；面板分别显示状态、今日观看分钟、已领数量。
- **设备码登录**：点「添加账号」→ 得到 8 位验证码 → 任意设备打开链接输入验证码即可，不用密码、不用 cookie。
  也支持直接粘贴已有的 access token。
- **按游戏过滤**：账号可绑定游戏（如 `apex`）。不选则跟随面板「默认游戏」设置。
- **自动选活动**：拉取账号可参与的掉宝活动（`ACTIVE` / `UPCOMING`），跳过未绑定账号的、已领完的。
- **自动选频道**：优先用活动允许的频道列表（ACL），并校验该频道正在播的正是活动对应游戏；
  没有 ACL 时按游戏目录挑在线且开了掉宝的频道。
- **自动观看**：每 20 秒刷新播放列表，对分片发 HTTP `HEAD`（不下载内容、不占带宽），推进观看时长。
- **自动领取**：进度达标且活动仍在可领期（结束后 24 小时内）自动领取，成功写库并留日志。
- **断线自愈**：token 过期自动刷新；频道下播自动换台；活动结束后进入待机。
- **日志**：面板「日志」页看实时日志，可按账号筛选。

## 模式

| 模式 | 行为 |
| --- | --- |
| `farm`（挖矿） | 自动选频道、观看、领取 |
| `monitor`（监控） | 只扫描活动与进度，不观看 |

## 架构

```
浏览器 ──▶ 反向代理(Caddy, /drops/*) ──▶ uvicorn 127.0.0.1:9200 (FastAPI)
                                             │
                                             ├─ manager：每账号一个 asyncio 任务
                                             ├─ miner：选活动 → 选频道 → 观看 → 领取
                                             ├─ twitch：Twitch GQL / OAuth / 播放列表
                                             └─ SQLite WAL：$DROPS_DATA_DIR/drops.db
```

Twitch 侧全部走官方客户端同款的 GQL 持久化查询与公开 OAuth 设备码流程，不修改客户端、不注入脚本、不需要浏览器。

## 目录

```
app/
  config.py     # 常量与 GQL 持久化查询定义
  db.py         # SQLite（账号 / 领取记录 / 设置 / 日志 / 观看时长）
  twitch.py     # Twitch 协议层：GQL、OAuth 设备码、播放列表与分片 HEAD
  miner.py      # 单账号掉宝循环：选活动 → 选频道 → 观看 → 领取
  manager.py    # 多账号任务管理 + 面板状态聚合
  web.py        # FastAPI 路由、密码登录、会话 cookie
  main.py       # 入口（uvicorn）
  static/       # 面板前端（原生 JS，无构建步骤）
deploy/
  twitch-drops.service   # systemd 单元
  Caddyfile.snippet      # 反向代理片段
```

运行时目录（不在仓库里）：

```
/opt/twitch-drops            # 程序 + venv
/var/lib/twitch-drops        # drops.db / INITIAL_PASSWORD.txt
```

## 从零安装（Ubuntu 22.04+ / Debian）

```bash
sudo apt update && sudo apt install -y python3-venv
sudo mkdir -p /opt/twitch-drops /var/lib/twitch-drops
sudo rsync -a --exclude venv ./ /opt/twitch-drops/
sudo chown -R "$USER":"$USER" /opt/twitch-drops /var/lib/twitch-drops

cd /opt/twitch-drops
python3 -m venv venv && ./venv/bin/pip install -r requirements.txt

# 先改 deploy/twitch-drops.service 里的 User 与 DROPS_PASSWORD
sudo cp deploy/twitch-drops.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now twitch-drops
```

反向代理：把 `deploy/Caddyfile.snippet` 的内容放进已存在的站点块，然后

```bash
caddy validate --config /etc/caddy/Caddyfile && sudo systemctl reload caddy
```

服务默认只监听 `127.0.0.1:9200`，不直接对外暴露；访问 `http://<你的服务器>/drops/`。
密码由 systemd 里的 `DROPS_PASSWORD` 指定，未指定则首次启动随机生成并写入
`$DROPS_DATA_DIR/INITIAL_PASSWORD.txt`。

## 本地开发

```bash
python3 -m venv venv && ./venv/bin/pip install -r requirements.txt
DROPS_DATA_DIR=./data DROPS_PASSWORD=devpass ./venv/bin/python -m app.main
# 打开 http://127.0.0.1:9200/
```

## 常用运维

```bash
sudo systemctl status twitch-drops      # 状态
sudo systemctl restart twitch-drops     # 重启
sudo journalctl -u twitch-drops -f      # 进程日志
```

## 环境变量

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `DROPS_HOST` | `127.0.0.1` | 监听地址 |
| `DROPS_PORT` | `9200` | 监听端口 |
| `DROPS_DATA_DIR` | `./data` | 数据目录（SQLite 与日志） |
| `DROPS_PASSWORD` | 随机 | 面板登录密码 |

## 注意

- 面板本身不含 TLS。请放在 HTTPS 反代后面，或只在可信网络使用；密码和会话 cookie 走明文 HTTP 是不安全的。
- Twitch 对同一 IP 下大量账号同时挖宝可能有限制。多账号时建议给账号配置各自的 HTTP 代理
  （账号的 `proxy` 字段，支持 `http://user:pass@host:port`）。
- 掉宝奖励需在活动结束后 24 小时内领取，否则失效。本程序会自动领取，但仍建议偶尔看一眼「活动」页。
- 仅供个人学习与自用，请遵守 Twitch 服务条款，风险自负。

## 协议来源

Twitch 侧的实现细节参考了这些开源项目，感谢作者：

- [DevilXD/TwitchDropsMiner](https://github.com/DevilXD/TwitchDropsMiner) —— GQL 持久化查询 hash、观看计时方式
- [ohne-b/twitch-drops-miner](https://github.com/ohne-b/twitch-drops-miner) —— TV 客户端 client_id 与设备码登录
- [grubdrops/grubdrops](https://github.com/grubdrops/grubdrops) —— 多账号 Web 面板形态

## License

MIT
