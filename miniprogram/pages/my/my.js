const app = getApp();

Page({
  data: {
    userId: '',
    nickName: '微信用户',
    avatarUrl: '/images/logo.jpg',
    fishTokens: 0,
    freeMode: false,
    historyList: [],
    processingCount: 0,
    creditRecordCount: 2,
    videoTasksToday: 0,
    maxVideoTasks: 3,
    showCreditModal: false
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
    this.setData({
      userId: app.globalData.userId || 'PX-8A2F90B1',
      fishTokens: app.globalData.fishTokens || 0,
      historyList: app.globalData.historyList || [],
      videoTasksToday: app.globalData.videoTasksToday || 0,
      maxVideoTasks: app.globalData.maxVideoTasks || 3
    });
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

  onOpenCreditModal() {
    this.setData({ showCreditModal: true });
  },

  onCloseCreditModal() {
    this.setData({ showCreditModal: false });
  },

  onFreeClaim() {
    const total = app.addToken(5);
    this.setData({
      fishTokens: total,
      showCreditModal: false
    });
    wx.showToast({
      title: '已领取 +5 算力',
      icon: 'success'
    });
  },

  onWatchVideo() {
    if (this.data.videoTasksToday >= this.data.maxVideoTasks) {
      wx.showToast({
        title: '今日补给次数已达上限',
        icon: 'none'
      });
      return;
    }

    wx.showLoading({ title: '正在调取补给影像…' });

    // 模拟广告播放体验 (2 秒后完成并发放奖励)
    setTimeout(() => {
      wx.hideLoading();
      const success = app.recordVideoTask();
      if (success) {
        this.refreshUserData();
        wx.showModal({
          title: '补给完成',
          content: '已成功注入 +10 算力！今日已观看 ' + this.data.videoTasksToday + '/' + this.data.maxVideoTasks,
          showCancel: false,
          confirmText: '太棒了'
        });
      }
    }, 1800);
  },

  onScrollToWorks() {
    // 聚焦或查看作品
    if (this.data.historyList.length === 0) {
      this.onGoCreate();
    }
  },

  onGoCreate() {
    wx.switchTab({
      url: '/pages/index/index'
    });
  },

  onPreviewWork(e) {
    const index = e.currentTarget.dataset.index;
    const item = this.data.historyList[index];
    if (!item) return;

    const urls = [];
    if (item.result) urls.push(item.result);
    if (item.original) urls.push(item.original);

    wx.previewImage({
      current: item.result,
      urls: urls.length ? urls : [item.result]
    });
  },

  onClearWorks() {
    wx.showModal({
      title: '清空作品记录',
      content: '确定要清除所有本地已保存的作品留存记录吗？',
      confirmText: '清空',
      confirmColor: '#9e4b3c',
      cancelText: '保留',
      success: (res) => {
        if (res.confirm) {
          app.clearHistory();
          this.refreshUserData();
          wx.showToast({ title: '已清空作品', icon: 'success' });
        }
      }
    });
  },

  onOpenOrders() {
    wx.showModal({
      title: '积分与订单档案',
      content: `当前可用算力：${this.data.fishTokens} 点\n已累计创作：${this.data.historyList.length} 次\n\n废片新生所遵循透明算力机制，每一次修复均有迹可循。`,
      showCancel: false,
      confirmText: '我知道了'
    });
  },

  onOpenInvite() {
    wx.showModal({
      title: '邀请好友得算力',
      content: '分享小程序给好友，好友首次创作，双方各获赠 20 点新生算力。',
      confirmText: '立即分享',
      cancelText: '暂不',
      success: (r) => {
        if (r.confirm) {
          wx.showToast({ title: '请点击右上角【···】分享', icon: 'none' });
        }
      }
    });
  },

  onOpenPrivacy() {
    wx.showModal({
      title: '隐私与安全保障',
      content: '【严守隐私底线】\n1. 上传的照片仅用于本次 AI 修复与微调处理；\n2. 处理完成后图片在临时沙盒中仅留存必要缓存；\n3. 绝不会将您的原始照片用于公开展示或模型二次训练。',
      showCancel: false,
      confirmText: '确认悉知'
    });
  },

  onOpenRetention() {
    wx.showModal({
      title: '照片保存期限说明',
      content: '为保障存储安全与隐私合规：\n\n• 服务端生成的结果图片将在 24 小时后自动滚动清除；\n• 请在生成完毕后及时将满意的拯救照片保存到手机相册中。',
      showCancel: false,
      confirmText: '明白'
    });
  },

  onContactSupport() {
    wx.showActionSheet({
      itemList: ['在线客服', '意见与风格定制反馈', '常见问题解答'],
      success: (res) => {
        if (res.tapIndex === 0) {
          wx.showModal({
            title: '专属客服',
            content: '客服工作时间：工作日 09:30 - 21:00\n如有问题或技术支持，可随时留言反馈。',
            showCancel: false,
            confirmText: '我知道了'
          });
        } else if (res.tapIndex === 1) {
          wx.showToast({ title: '感谢您的支持与反馈', icon: 'success' });
        } else {
          this.onOpenRetention();
        }
      }
    });
  },

  onClearStorage() {
    wx.showModal({
      title: '清理缓存',
      content: '将清除本地临时图像缓存，但会保留您的算力余额。确定清理吗？',
      confirmText: '确认清理',
      confirmColor: '#9e4b3c',
      cancelText: '取消',
      success: (res) => {
        if (res.confirm) {
          wx.clearStorage({
            success: () => {
              // 恢复基础数据
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
