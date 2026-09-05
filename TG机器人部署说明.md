# Telegram 解析机器人 MVP

当前版本包含：

- Telegram Bot 基础程序
- 指定频道关注验证
- 抖音/小红书链接识别
- Render 免费 Worker 部署配置
- Token 和频道配置使用环境变量，不写入代码

## 本地测试

PowerShell：

```powershell
$env:TELEGRAM_BOT_TOKEN = "你的 Bot Token"
$env:REQUIRED_CHANNEL = "@你的频道用户名"
python -m pip install -r tg_parser_requirements.txt
$env:RENDER_EXTERNAL_URL = "https://你的服务名.onrender.com"
python tg_parser_bot.py
```

机器人必须是目标频道管理员，否则无法检查用户是否关注。

## Render 部署

1. 把项目上传到 GitHub。
2. 在 Render 创建 Web Service，选择 Free。
3. Build Command：`pip install -r tg_parser_requirements.txt`
4. Start Command：`python tg_parser_bot.py`
5. 添加环境变量：`TELEGRAM_BOT_TOKEN`、`REQUIRED_CHANNEL`。Render 会为 Web Service 提供 `RENDER_EXTERNAL_URL`；若未自动提供，请将它设为服务的完整 HTTPS 地址。

免费实例可能休眠或重启，临时文件不会永久保存。

## 当前边界

此 MVP 不收集登录 Cookie，也不绕过平台访问控制。后续媒体适配器应只处理公开且用户拥有或获授权的内容。
