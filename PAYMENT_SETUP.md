# 微信虚拟支付与光子权益充值

使用道具直购 `short_series_goods`，不使用代币充值。新版本接入签名下单、微信收银台、服务端查单、幂等发放、后台补单与退款对账。支付开关默认关闭；模拟测试通过不等于真实支付验收通过。

## 部署与开通

1. 更新后端，重新编译、上传新版小程序。
2. 微信后台发布商品：points_600=6元/600光子，points_3300=30元/3300光子，points_8160=68元/8160光子，points_16640=128元/16640光子。到账数量已包含赠送；生成档位收费以后台轻量/精细配置为准。
3. 微信消息推送沿用 `https://image.myil.top/api/wxpush`，使用原推送 Token，明文 XML/JSON 均支持。启用道具发货 `xpay_goods_deliver_notify` 和退款 `xpay_refund_notify`。同一入口继续处理图片审核，不要覆盖成仅接收支付的地址。
4. 后台「平台与安全」确认 AppID/AppSecret、OfferID、对应 AppKey、推送 Token，勾选「启用光子充值」，统一保存。个人主体按官方指引使用现网 env=0；沙箱仅用于平台实际支持的主体。本轮没有发起沙箱或现网真实支付。
5. 小程序订单中心 path 填 `pages/orders/orders`，不是网址，也没有文件扩展名。先上传包含此页面的版本，再填写审核表。光子中心提供「充值订单 / 核对到账」入口。

## 资金与订单规则

- 客户端成功、失败或取消不直接改余额；服务器查询微信确认业务单号、金额、环境、平台单号后才发放。
- 订单、流水、余额在同一个 SQLite 事务内写入，重复回调、并发补单和重启不会重复发放。
- 下单超时不自动再下单；已登记订单保留在订单中心。核对、补单不再次扣款。
- 退款以微信确认的累计金额为准，按累计比例回收光子（含赠送）。已使用权益形成可追踪的负余额，不直接抹掉欠额。
- 商户主动退款在微信后台操作；代码不自动发起真实退款。iOS 退款由 Apple 决定，本版不自动裁决退款问询；实际退款完成通知及主动查单参与对账。
- 关闭支付开关只阻止新下单，原有订单继续核对与退款对账。
- 光子只用于本项目生成权益，不转账、不提现。

## 验收

本轮使用隔离账户、模拟微信接口和 SDK，无真实扣款、退款或新增图片生成。
上线后由用户在手机微信主动确认小额订单：收银台金额、发货推送、余额只增加一次、订单中心及微信账单保持一致。真实收银台、回调可达和实际退款尚未实测。

依据：[个人虚拟支付](https://developers.weixin.qq.com/miniprogram/dev/platform-capabilities/business-capabilities/virtual-payment/person.html)、[客户端 API](https://developers.weixin.qq.com/miniprogram/dev/api/payment/wx.requestVirtualPayment.html)、[查单 API](https://developers.weixin.qq.com/miniprogram/dev/server/API/VirtualPayment/api_query_order)。
