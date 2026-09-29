const app = getApp();

Page({
  data: {
    inviteCode: 'PX-INVITE92'
  },

  onLoad() {
    const uid = app.globalData.userId || 'PX-E71C756FB696';
    this.setData({
      inviteCode: uid.replace('PX-', 'INV-')
    });
  },

  onCopyCode() {
    wx.setClipboardData({
      data: this.data.inviteCode,
      success: () => {
        wx.showToast({ title: '邀请码已复制', icon: 'success' });
      }
    });
  },

  onShareAppMessage() {
    return {
      title: '送你 30 点算力，来废片新生所拯救你的废旧照片！',
      path: '/pages/index/index?invite=' + this.data.inviteCode,
      imageUrl: '/images/logo.jpg'
    };
  }
});
