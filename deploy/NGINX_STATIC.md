# 宝塔：公开网页文件交给 Nginx

## 在宝塔终端进入服务器项目目录，以 root 执行

```bash
bash .update
bash deploy/install-nginx-static.sh --bt
```

默认域名是 `image.myil.top`，接口端口为 `8000`。脚本优先使用宝塔 Nginx `/www/server/nginx/sbin/nginx`，站点配置为 `/www/server/panel/vhost/nginx/image.myil.top.conf`。如果面板的站点名称/文件名不同，使用实际文件：

```bash
bash deploy/install-nginx-static.sh --bt --site /www/server/panel/vhost/nginx/实际站点.conf --domain 实际域名 --port 8000
```

脚本只往对应的现有 HTTPS `server` 块添加托管 include，保留证书、原反向代理、接口、后台、账户及根路径微信校验/回调。配置先备份，再 `nginx -t`，通过后热重载；检测或重载失败时恢复配置原字节。已有同名静态 location 冲突会停止并恢复，不覆盖原站点配置。

公开文件单独导出到 `/var/www/photo-rescue-static`，不将项目根目录、数据库、上传目录或密钥交给 Nginx。文件名白名单与公开源目录一起约束导出。脚本自动检查 gzip_static 模块，没有该模块时保留文本 gzip。HTML 要重新验证，带内容版本的 JS/CSS/SVG 缓存一年；旧版本文件保留，已打开页面可以继续加载。

以后正常 `bash .update` 会自动同步这个默认公开目录，不需重复添加 include。使用自定义 `--public-root` 时，在更新后再次执行相同部署命令刷新对应目录。

## 查看接入结果

浏览器开发者工具 → 网络 → 任一 `/web-assets/十六位版本/app.js` 响应应有：

- `X-Static-Delivery: nginx`（未接入时后端兼容服务是 `backend-public-assets`）。
- `Cache-Control: public, max-age=31536000, immutable`。
- 浏览器允许 gzip 时有 `Content-Encoding: gzip`；JS 类型为 `text/javascript`。

```bash
curl -I --compressed https://image.myil.top/
```

首页应有 `X-Static-Delivery: nginx`，并保持 `no-cache, must-revalidate`。`/api/*`、`/admin`、用户响应不放进公开目录，也不设置公共代理缓存。接入后检查登录、生成、作品、社区和微信根路径校验仍走原后端。

## COS 与服务器各自负责什么

- 网页和小程序的个人图片下载原本就直连 COS，后端返回 JSON 和短期签名 URL，不转发图片字节。管理员旧宣传帖上传到本地的公开媒体仍走原媒体接口；这类列表新增有界缓存的小图接口，未改为私人图片中转。
- 将高清图放在小卡片里仅改变显示尺寸。本次新增列表专用的 **480×480 范围、只缩小、JPEG 质量 70** 变体，COS 在下载时处理。处理参数参与签名，不能剥掉参数改变变体。
- 原图、高清成品、详情放大和保存不变；列表缩略与高清缓存分别保存。
- 数据万象缩图需要桶开通基础图片处理和对应权限，使用情况/计费在腾讯云控制台检查。失败时客户端只从 COS 取高清兜底，不增加服务器图片中转。

## 2 核 / 2 GB / 4 Mbps 的减负边界

已优化：按账户/状态定位作品、分页与计数共享一次快照、批量读取结算；社区仅解码当前页；历史流水在 SQLite 排序分页；公开配置窄读取与有界短缓存；网页重复 GET 合并，卡片复用，后台隐藏暂停轮询，长任务放慢并退避，每轮只读状态请求最多等 15 秒且不越过绝对轮询期限；公开文件预压缩、内容版本缓存与小 SVG 图标。

Nginx 静态直出降低 Python 服务开销，**不会把 4 Mbps 变成更大带宽**。图片流量已交给 COS；首次网页代码/接口依然使用这 4 Mbps。若监测到带宽持续跑满，下一步把公开网页资源放到 CDN/COS，或增加带宽；若 CPU/内存、接口延时或生成队列先达到瓶颈，则按实际指标扩容。离线专项结果不能换算成线上可承载人数。

## 撤销宝塔静态接入

在面板「网站 → 对应站点 → 配置文件」仅移除 `# photo-rescue-managed-static` 及紧随的 `include "/var/www/photo-rescue-static/nginx-web.conf";` 两行，保存并检测/热重载。后端保留公开文件兼容服务。也可恢复脚本输出的站点备份（恢复前核对其后有没有其他配置修改）。不删除用户数据库和上传文件。

参考：[宝塔配置文件路径](https://www.bt.cn/new/btcode)、[宝塔站点配置编辑](https://docs.bt.cn/user-guide/site/php/site-config/config-file/)、[Nginx gzip_static](https://nginx.org/en/docs/http/ngx_http_gzip_static_module.html)、[COS 缩放与开通](https://cloud.tencent.com/document/product/436/113295)、[处理参数签名](https://cloud.tencent.com/document/product/436/110496)。
