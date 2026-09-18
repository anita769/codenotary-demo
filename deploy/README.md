# CodeNotary 生产部署件
#
#   浏览器 → https://codenotary.example.com → [Nginx:443] → 127.0.0.1:18091（Console）
#   Agent 协作消息 → [Matrix homeserver]（可选，团队旁观频道）
#
# 原则：gateway/console 只监听 127.0.0.1；Nginx 是唯一门面；
# 写操作一律 capability token（tools/notary_token.py 签发）。

## 1. 服务（systemd，挂了自动拉起）

- `codenotary-gateway.service` — 网关，127.0.0.1:18090
- `codenotary-console.service` — 前端，127.0.0.1:18091

安装：
  sudo cp systemd/*.service /etc/systemd/system/
  sudo systemctl daemon-reload
  sudo systemctl enable --now codenotary-gateway codenotary-console

## 2. Nginx

`nginx/codenotary.conf`：把 `codenotary.xxx.com` 替换为真实域名后：
  sudo cp nginx/codenotary.conf /etc/nginx/sites-available/codenotary
  sudo ln -s /etc/nginx/sites-available/codenotary /etc/nginx/sites-enabled/
  sudo certbot --nginx -d codenotary.xxx.com   # 需 DNS 已指向本机且 80 端口开放
  sudo nginx -t && sudo systemctl reload nginx

## 3. 现场断网回退（关键！）

笔记本 /etc/hosts 加一行：
  127.0.0.1  codenotary.xxx.com
- 有网：删掉/注释该行 → 走服务器
- 断网：启用该行 + 本地起 console（tools/notary_console.py）→ 同一 URL 照开

## 4. 安全检查单（上线前过一遍）

- [ ] `ss -tln | grep 1809` 只显示 127.0.0.1，没有 0.0.0.0
- [ ] `curl https://codenotary.xxx.com/api/gw` 返回 ok
- [ ] 不带 token 调 /api/adjudicate/* 返回 403
- [ ] 证书有效期 > 比赛日（`echo | openssl s_client -connect codenotary.xxx.com:443 2>/dev/null | openssl x509 -noout -dates`）
- [ ] 拔网线演练：/etc/hosts 回退 + 本地 console + 录屏，完整过一遍主线
