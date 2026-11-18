# SSH 加固模板

`00-no-password.conf` 放到 `/etc/ssh/sshd_config.d/`：禁止密码登录，root 只能用密钥。

`authorized_keys` 和任何私钥都不要提交到 Git。
