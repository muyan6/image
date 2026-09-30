const app = getApp();
const api = require('../../utils/api.js');

/**
 * 激励视频广告配置全部来自后端 GET /api/config 的 ads 字段，
 * 在 /admin「通用设置 → 激励视频广告」里维护：
 *   开关、广告位、可信服务端验证服务和密钥均已配置
 *   => 后端返回 ads.rewarded_video_ready = true
 *
 * ready = false（未开启 / 广告位 ID 为空）时：
 *   - 本页不渲染「看视频得光子」任务卡（见 my.wxml 的 wx:if）；
 *   - 后端 POST /api/me/earn (kind=video) 也会直接 403。
 *
 * 旧的「模拟动画兜底」（REWARDED_AD_UNIT_ID 为空时，点一下等 1.5 秒
 * 就直接 +10 光子）已删除 —— 那正是"没配广告却直接到账"的原因。
 */
const DEFAULT_VIDEO_REWARD = 10;
const DEFAULT_MAX_VIDEO_TASKS = 3;

Page({
  data: {
    userId: '',
    nickName: '微信用户',
    avatarUrl: '/images/logo.jpg',
    lightPoints: 0,
    freeMode: false,
    priceLight: 40,
    priceFine: 40,
    historyList: [],
    previewWorks: [],
    processingCount: 0,
    creditRecordCount: '—',
    videoTasksToday: 0,
    maxVideoTasks: DEFAULT_MAX_VIDEO_TASKS,
    videoReward: DEFAULT_VIDEO_REWARD,
    // 广告是否真的可用（由后端判定），未配置时整块任务卡隐藏
    videoAdReady: false
  },

  onLoad() {
    // tabBar 页由 onShow 首次加载并在之后每次返回时同步。
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
    const version = (this._profileLoadVersion || 0) + 1;
    this._profileLoadVersion = version;
    this.setData({
      userId: app.globalData.userId || '登录后显示',
      lightPoints: app.globalData.lightPoints || 0,
      freeMode: !!app.globalData.freeMode,
      historyList: list,
      previewWorks: list.slice(0, 3),
      processingCount: list.filter(w => w.status === 'processing').length
    });

    // 广告位配置（后台热改即时生效）：决定「看视频得光子」入口是否显示
    api.config().then((c) => {
      if (!c) return;
      const ads = c.ads || {};
      this._videoAdUnitId = ads.rewarded_video_unit_id || '';
      const prices = c.prices || {};
      app.globalData.freeMode = !!c.free_mode;
      this.setData({ videoAdReady: !!ads.rewarded_video_ready,
        freeMode: !!c.free_mode,
        priceLight: prices.light != null ? prices.light : this.data.priceLight,
        priceFine: prices.fine != null ? prices.fine : this.data.priceFine });
    }).catch(() => {});

    // 新设备或清缓存后，以云端任务作为作品数量与状态的事实来源。
    api.myJobs(100).then((result) => {
      if (version !== this._profileLoadVersion || !result || !Array.isArray(result.jobs)) return;
      const prior = app.globalData.historyList || [];
      const byId = new Map(prior.filter(w => w.jobId).map(w => [w.jobId, w]));
      const localOnly = prior.filter(w => !w.jobId);
      const cloud = result.jobs.map(j => Object.assign({}, byId.get(j.id) || {}, {
        jobId: j.id, original: api.absolute(j.orig_url || ''),
        result: j.status === 'succeeded' ? api.absolute(j.result_url || '') : '',
        preview: j.status === 'succeeded' &&
          (typeof api.isJobCosUrl !== 'function' || api.isJobCosUrl(j.result_url)) ? api.absolute(j.result_url || '') : '',
        status: j.status, quality: j.quality, provider: j.provider,
        templateName: j.template_name || '', createdAt: j.created_at || 0
      }));
      const merged = localOnly.concat(cloud).sort((a,b) => (b.createdAt || 0) - (a.createdAt || 0));
      app.globalData.historyList = merged;
      app.persist();
      this.setData({ historyList: merged, previewWorks: merged.slice(0,3),
        processingCount: merged.filter(w => w.status === 'processing').length });
    }).catch(() => {});

    // 光子余额 / 视频补给进度以服务端为准
    api.me().then((d) => {
      if (!d) return;
      if (typeof d.balance === 'number') app.setBalance(d.balance);
      this.setData({
        userId: d.user_id || app.globalData.userId || '登录后显示',
        lightPoints: app.globalData.lightPoints,
        videoTasksToday: (d.earn && d.earn.video_today) || 0
      });
    }).catch(() => {});
  },

  onHide() {
    this._profileLoadVersion = (this._profileLoadVersion || 0) + 1;
  },

  onCopyUserId() {
    if (!this.data.userId || this.data.userId === '登录后显示') {
      wx.showToast({title: '正在获取账号编号，请稍后重试', icon: 'none'});
      return;
    }
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

  /** 看视频补给：必须走真实激励视频；广告位没配好则本入口不显示。 */
  onWatchVideo() {
    if (!this.data.videoAdReady || !this._videoAdUnitId) {
      wx.showToast({ title: '激励视频广告暂未开放', icon: 'none' });
      return;
    }
    if (this.data.videoTasksToday >= this.data.maxVideoTasks) {
      wx.showToast({
        title: '今日补给次数已达上限',
        icon: 'none'
      });
      return;
    }
    if (this._videoBusy) return;
    this._videoBusy = true;
    this._playRewardedAd();
  },

  /** onClose only triggers a request; a trusted platform receipt decides credit. */
  _playRewardedAd() {
    const unitId = this._videoAdUnitId;
    if (!unitId) return;
    if (this._rewardedAd && this._rewardedAdUnitId !== unitId) {
      if (this._rewardedAd.destroy) this._rewardedAd.destroy();
      this._rewardedAd = null;
    }
    if (!this._rewardedAd) {
      this._rewardedAdUnitId = unitId;
      this._rewardedAd = wx.createRewardedVideoAd({ adUnitId: unitId });
      this._rewardedAd.onError((err) => {
        console.warn('激励视频加载失败', err);
        wx.showToast({ title: '广告暂时拉取失败，稍后再试', icon: 'none' });
      });
    }
    const ad = this._rewardedAd;
    const onClose = (res) => {
      ad.offClose(onClose);
      this._videoBusy = false;
      if (res && res.isEnded) {
        // A platform/bridge receipt is independently verified on the backend.
        this._grantVideoReward(res.receipt || res.transactionId || '');
      } else {
        wx.showToast({ title: '完整观看才能领取补给哦', icon: 'none' });
      }
    };
    ad.onClose(onClose);
    ad.show().catch(() => {
      // 广告拉取失败时先 load 再 show 一次
      ad.load().then(() => ad.show()).catch(() => {
        ad.offClose(onClose);
        this._videoBusy = false;
        wx.showToast({ title: '广告暂时拉取失败，稍后再试', icon: 'none' });
      });
    });
  },

  /** 向服务端领取视频奖励（次数、发放与广告开关都在服务端校验） */
  _grantVideoReward(receipt) {
    if (!receipt) {
      wx.showToast({title: '广告观看凭据待验证，奖励尚未到账', icon: 'none'});
      return;
    }
    api.earn('video', receipt)
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
