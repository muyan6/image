# 网站注册、手机网页与微信绑定

## 现在可直接使用

- 网页使用与小程序相同的模板、作品、社区、光子流水和生成接口，手机提供底部四个入口：创作、模板、社区、我的。
- 普通访客可看公开模板和社区宣传内容；创作、个人作品及社区写操作要求先注册登录。匿名浏览不创建用户行。
- 网站注册使用用户名和密码，初始余额固定为 **0**，不跟随小程序新人赠送配置。网站会话没有签到、视频、邀请奖励或在线购买加点入口。
- 管理员在 `/admin` →「账号管理」选择网站注册账号，点击「手动改光子」，输入 `+50` 或目标余额。实际增减与余额在同一事务中记录，网站光子明细立即可核对。
- 管理员可重置网站密码；重置后原网站会话失效。不会重置作品、余额或小程序会话。
- 旧匿名网页账户的原记录保留，不自动升级成注册账户，不按浏览器 Cookie 猜测归属，也不计入网站注册人数。

## 部署

1. 备份运行中的用户数据库、任务数据库、清理数据库和设置文件，保持原 `DATA_DIR` / `UPLOAD_DIR` 持久化目录。
2. 更新后端并重启。更新脚本与两个 Dockerfile 自动生成带内容版本的网页与 gzip 文件；`/web-assets` 仅服务公开网页资源，不暴露后端文件。宝塔静态接入见 [Nginx 静态部署](../deploy/NGINX_STATIC.md)。
3. 网站实际域名默认 `https://image.myil.top`。其他域名设置 `WEB_PUBLIC_ORIGIN=https://实际域名`。反向代理保留原 Host；生产会话 Cookie 使用 Secure、HttpOnly、SameSite。
4. 在 COS 设置网站实际 Origin 的 CORS，允许实际使用的 `PUT / GET / HEAD` 和 `Content-Type` 请求头。网页图片字节直连 COS；旧个人图片只通过 JSON 修复后取得 COS 地址，不使用业务服务器图片中转。
5. 打开网站，在手机宽度检查注册、选图、模板、文字生图、生成费用、作品、社区、明细及密码/退出操作。
6. 微信绑定暂未配置也能注册和独立使用网站；网站显示清晰的未开通说明。

网站图片审核不向微信提交 `web-*` 身份，使用已有腾讯云云端审核通道；网站留言在没有合法微信身份时使用腾讯文本审核。审核服务未就绪或返回非通过结果时，留言不会公开。

## 微信绑定接入准备

用户当前没有已认证公众号或开放平台网站应用，因此默认保持关闭。未来在 `/admin` →「平台与安全」→「网站注册与微信绑定」配置：

- **公众号网页授权**：微信内手机网页，使用公众号 AppID / AppSecret。
- **开放平台网站应用**：浏览器扫码，使用网站应用 AppID / AppSecret。
- 网站公开 HTTPS 地址；回调固定为 `/api/auth/site/wechat/callback`。

公众号/网站应用与小程序需要关联同一开放平台。服务端验证微信 code 后取得 UnionID，再匹配小程序服务端已验证的 UnionID 和 AppID。不同应用的 OpenID 不做猜测合并。

绑定过程要求同一已注册网站会话和一次性、短期的授权 state。新绑定使用配置快照，配置变更会使旧 state 失效。找不到已有小程序证明时，仅保存“待小程序身份验证”状态，不新建带赠送的微信账户。后续真实小程序登录取得同一 UnionID 后完成绑定；此路径不再领新人赠送。

绑定后：

- 网站和小程序使用同一个余额。已有网站余额仅转入一次，原网站余额归零，转账保留真实审计；不存在额外赠送。
- 旧网页与小程序作品、扣款/退款、充值历史、投稿、留言、喜欢、模板偏好都按共享主体读取，旧记录不删除。
- 旧任务完成/退款及新扣款均落到同一余额，授权只认真实关联的所有者；不关联第三方账号。
- 网站浏览器草稿/提交编号使用稳定的网站注册身份，不因绑定后的共享编号变化丢失。
- 网站渠道仍遵守手动补给规则。原小程序的合法渠道能力保持兼容。

没有正式微信凭据时，离线验证覆盖的是协议、身份匹配、并发、事务、退款、权限和跨端读取，不代表已经进行真实微信授权。

## 本地验收

后端测试使用合成图片、临时 SQLite 和模拟外部响应；不使用生产账号或收费调用。

```powershell
$env:PYTHONIOENCODING='utf-8'
npm ci --prefix backend/tests --ignore-scripts
python backend/tests/run_review.py
```

DOM 测试依赖 `backend/tests/package.json` 锁定的 jsdom。也可设置 `WEB_DOM_MODULE` 指向已有 jsdom 的绝对目录；生产网站不依赖 Node/npm/CDN。

新增专项：`test_site_accounts.py`、`test_account_sync.py`、`test_site_community_sync.py`、`test_web_creation_parity.cjs`、`test_web_library_parity.cjs`、`test_web_shell.cjs`。

负载专项：`test_load_backend.py`、`test_load_media.py`、`test_load_media_frontend.cjs`、`test_load_web.cjs`、`test_load_static.py`。公开模板与社区只短期缓存非个性化展示数据；私人列表按当前账户隔离，切换/退出清除。余额、价格、支付和生成提交不走展示缓存。列表使用 COS 签名缩略图，详情放大与保存继续使用高清图；COS 处理失败只尝试 COS 高清地址，不经业务服务器中转。

密码使用独立随机盐与 PBKDF2-SHA256 600,000 次计算，随机网站会话只在数据库保留摘要。参数依据：[OWASP Password Storage](https://cheatsheetseries.owasp.org/cheatsheets/Password_Storage_Cheat_Sheet.html)。微信接入参考：[网站微信登录](https://developers.weixin.qq.com/doc/oplatform/Website_App/WeChat_Login/Wechat_Login.html)、[公众号网页授权](https://developers.weixin.qq.com/doc/offiaccount/OA_Web_Apps/Wechat_webpage_authorization.html)、[UnionID 机制](https://developers.weixin.qq.com/miniprogram/dev/framework/open-ability/union-id.html)。
