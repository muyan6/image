const app = getApp();
const api = require('../../utils/api.js');

/**
 * 微信官方激励式视频广告位 ID。
 * 开通条件：mp.weixin.qq.com → 流量主（累计独立访客 UV ≥ 1000）
 * → 广告位管理 → 新建「激励式视频广告」，把 ad-unit-id 填在这里。
 * 填好后"看视频补给"就走真实广告（微信官方结算分成）；留空则用模拟动画。
 */
const REWARDED_AD_UNIT_ID = '';

Page({
  data: {
    userId: '',
    nickName: '微信用户',
    avatarUrl: '/images/logo.jpg',
    lightPoints: 0,
    freeMode: false,
    historyList: [],
    previewWorks: [],
    videoTasksToday: 0,
    maxVideoTasks: 3,
    videoReward: 10
  },

  onLoad() {
    this.refreshUserData();
  },

  onShow() {
    this.refreshUserData();
  },

  onPullDownRefresh() {
    this.refreshUserData();
    wx.stopPullDownRefresh();
  },

  refreshUserData() {
    const list = app.globalData.historyList || [];
    this.setData({
      userId: app.globalData.userId || 'PX-8A2F90B1',
      lightPoints: app.globalData.lightPoints || 0,
      freeMode: !!app.globalData.freeMode,
      historyList: list,
      previewWorks: list.slice(0, 3)
    });

    // 光子余额 / 视频补给进度以服务端为准
    api.me().then((d) => {
      if (!d) return;
      if (typeof d.balance === 'number') app.setBalance(d.balance);
      this.setData({
        lightPoints: app.globalData.lightPoints,
        videoTasksToday: (d.earn && d.earn.video_today) || 0
      });
    }).catch(() => {});
  },

  onCopyUserId() {
    wx.setClipboardData({
      data: this.data.userId,
      success: () => {
        wx.showToast({
          title: '已复制档案编号',
          icon: 'success'
        });
      }
    });
  },

  onEditProfile() {
    wx.showToast({
      title: '已绑定微信身份',
      icon: 'none'
    });
  },

  /* 二级页面跳转 */
  onGoCredits() {
    wx.navigateTo({
      url: '/pages/credits/credits'
    });
  },

  onGoWorks() {
    wx.navigateTo({
      url: '/pages/works/works'
    });
  },

  onGoInvite() {
    wx.navigateTo({
      url: '/pages/invite/invite'
    });
  },

  onGoPrivacy() {
    wx.navigateTo({
      url: '/pages/privacy/privacy'
    });
  },

  onGoRetention() {
    wx.navigateTo({
      url: '/pages/retention/retention'
    });
  },

  onGoCreate() {
    wx.switchTab({
      url: '/pages/index/index'
    });
  },

  /** 看视频补给：服务端记账（3 次/天，每次 +10 光子）。
   *  配了 REWARDED_AD_UNIT_ID 走微信官方激励视频；否则用模拟动画。 */
  onWatchVideo() {
    if (this.data.videoTasksToday >= this.data.maxVideoTasks) {
      wx.showToast({
        title: '今日补给次数已达上限',
        icon: 'none'
      });
      return;
    }

    if (REWARDED_AD_UNIT_ID && wx.createRewardedVideoAd) {
      this._playRewardedAd();
      return;
    }

    // 模拟流程（未开通流量主时）
    wx.showLoading({ title: '正在调取补给影像…' });
    setTimeout(() => {
      this._grantVideoReward();
    }, 1500);
  },

  /** 微信官方激励式视频：完整观看后 onClose 里 isEnded 才发奖 */
  _playRewardedAd() {
    if (!this._rewardedAd) {
      this._rewardedAd = wx.createRewardedVideoAd({ adUnitId: REWARDED_AD_UNIT_ID });
      this._rewardedAd.onError((err) => {
        console.warn('激励视频加载失败', err);
        wx.showToast({ title: '广告暂时拉取失败，稍后再试', icon: 'none' });
      });
    }
    const ad = this._rewardedAd;
    const onClose = (res) => {
      ad.offClose(onClose);
      if (res && res.isEnded) {
        this._grantVideoReward();
      } else {
        wx.showToast({ title: '完整观看才能领取补给哦', icon: 'none' });
      }
    };
    ad.onClose(onClose);
    ad.show().catch(() => {
      // 广告拉取失败时先 load 再 show 一次
      ad.load().then(() => ad.show()).catch(() => {
        ad.offClose(onClose);
        wx.showToast({ title: '广告暂时拉取失败，稍后再试', icon: 'none' });
      });
    });
  },

  /** 向服务端领取视频奖励（次数与发放都在服务端校验） */
  _grantVideoReward() {
    wx.hideLoading();
    api.earn('video')
      .then((d) => {
        if (d && typeof d.balance === 'number') app.setBalance(d.balance);
        this.refreshUserData();
        wx.showModal({
          title: '补给完成',
          content: '已成功注入 ✦' + this.data.videoReward + ' 光子！今日已观看 ' +
            d.count_today + '/' + this.data.maxVideoTasks,
          showCancel: false,
          confirmText: '太棒了'
        });
      })
      .catch((err) => {
        wx.showToast({ title: err.message || '补给失败，稍后再试', icon: 'none' });
      });
  },

  onContactSupport() {
    wx.showActionSheet({
      itemList: ['在线客服咨询', '意见与风格定制反馈', '照片保存期限说明'],
      itemColor: '#1a1917',
      success: (res) => {
        if (res.tapIndex === 0) {
          wx.showModal({
            title: '专属客服',
            content: '客服服务时间：09:30 - 21:00\n如有问题或技术支持，可随时联系。',
            showCancel: false,
            confirmText: '我知道了'
          });
        } else if (res.tapIndex === 1) {
          wx.showToast({ title: '感谢您的支持与反馈', icon: 'success' });
        } else {
          this.onGoRetention();
        }
      }
    });
  },

  /** 清理本地缓存：光子在服务端记账，清缓存不再丢余额 */
  onClearStorage() {
    wx.showModal({
      title: '清理缓存数据',
      content: '将清除本机的临时数据与历史记录缓存。光子余额保存在服务器，不受影响。确定清理吗？',
      confirmText: '确认清理',
      confirmColor: '#9e4b3c',
      cancelText: '取消',
      success: (res) => {
        if (res.confirm) {
          wx.clearStorage({
            success: () => {
              app.onLaunch();
              this.refreshUserData();
              wx.showToast({ title: '缓存清理完毕', icon: 'success' });
            }
          });
        }
      }
    });
  }
});
