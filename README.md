# 邮件转发到企业微信（Python）

该脚本会定时检查本地邮箱（IMAP）的新邮件，并把发送给“指定账户”的邮件摘要转发到企业微信机器人。

## 功能

- 连接 IMAP 邮箱并扫描 `UNSEEN` 新邮件
- 仅转发收件人（To/Cc/Delivered-To）中包含目标关键词的邮件
- 转发邮件主题、发件人、收件人、时间、正文摘要到企业微信
- 通过本地状态文件记录 `last_uid`，避免重复处理

## 使用步骤

1. 准备 Python 3.9+
2. 复制配置文件并填写：

   ```bash
   cp .env.example .env
   ```

3. 导出环境变量（可用 `set -a; source .env; set +a`）：

   ```bash
   set -a
   source .env
   set +a
   ```

4. 运行脚本：

   ```bash
   python3 wecom_mail_forwarder.py
   ```

## 关键配置说明

- `TARGET_ACCOUNT_KEYWORD`：用于匹配你要监听的账户（支持部分关键词匹配）
  - 例如设成 `alerts@company.com`
  - 当邮件的 `To/Cc/Delivered-To` 含该值时才会被转发
- `WECOM_WEBHOOK`：企业微信“群机器人”Webhook 地址

## 注意事项

- 建议使用邮箱的“客户端授权码”，不要直接使用网页登录密码。
- 当前实现以轮询方式工作（默认每 30 秒）。
- 若你希望转发附件，可在脚本中扩展 `fetch_message` 和 `send_wecom_markdown` 逻辑。
