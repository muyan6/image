const app = getApp();
const api = require('../../utils/api.js');

Page({
  data: {
    inviteCode: 'INV--------'
  },

  onLoad() {
    // 邀请码由服务端按 openid 派发（可真实兑奖），本地编号仅作兜底展示
    api.me()
      .then((d) => {
        if (d && d.invite_code) {
          this.setData({ inviteCode: d.invite_code });
        }
      })
      .catch(() => {
        const uid = app.globalData.userId || 'PX-E71C756F';
        this.setData({ inviteCode: uid.replace('PX-', 'INV-') });
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
      title: '送你 ✦30 光子，来废片新生所拯救你的废旧照片！',
      path: '/pages/index/index?invite=' + this.data.inviteCode,
      imageUrl: '/images/logo.jpg'
    };
  }
});
