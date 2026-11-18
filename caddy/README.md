# Caddy 配置模板

- `osaka.Caddyfile`：大阪，`sumax.duckdns.org:8443` → OpenList，`media.sumax.duckdns.org:8443` → mediahub。Let's Encrypt 证书由 Caddy 申请。
- `hangzhou.Caddyfile`：杭州裸 IP，自签证书，443 → OpenList，8443 → mediahub。

证书私钥不要放进仓库。
