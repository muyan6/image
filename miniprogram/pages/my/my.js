const app = getApp();
const api = require('../../utils/api.js');

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

  /** 看视频补给：服务端记账（3 次/天，每次 +10 光子），本地只做播放动效 */
  onWatchVideo() {
    if (this.data.videoTasksToday >= this.data.maxVideoTasks) {
      wx.showToast({
        title: '今日补给次数已达上限',
        icon: 'none'
      });
      return;
    }

    wx.showLoading({ title: '正在调取补给影像…' });

    setTimeout(() => {
      api.earn('video')
        .then((d) => {
          wx.hideLoading();
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
          wx.hideLoading();
          wx.showToast({ title: err.message || '补给失败，稍后再试', icon: 'none' });
        });
    }, 1500);
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
