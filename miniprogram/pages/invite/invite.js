const app = getApp();
const api = require('../../utils/api.js');

Page({
  data: {
    inviteCode: '',
    inviteError: false
  },

  onLoad() {
    this.loadInviteCode();
  },

  onShow() {
    if (!this.data.inviteCode) this.loadInviteCode();
  },

  loadInviteCode() {
    if (this._loadingCode) return;
    this._loadingCode = true;
    api.me()
      .then((d) => {
        const code = (d && d.invite_code) || '';
        if (!/^INV-[0-9A-F]{8}$/.test(code)) throw new Error('邀请码尚未就绪');
        this.setData({ inviteCode: code, inviteError: false });
      })
      .catch(() => {
        this.setData({ inviteCode: '', inviteError: true });
      }).finally(() => { this._loadingCode = false; });
  },

  onCopyCode() {
    if (!/^INV-[0-9A-F]{8}$/.test(this.data.inviteCode)) {
      this.loadInviteCode();
      wx.showToast({ title: '邀请码尚未获取，请稍后重试', icon: 'none' });
      return;
    }
    wx.setClipboardData({
      data: this.data.inviteCode,
      success: () => {
        wx.showToast({ title: '邀请码已复制', icon: 'success' });
      }
    });
  },

  onShareAppMessage() {
    if (!/^INV-[0-9A-F]{8}$/.test(this.data.inviteCode)) {
      return {title: '来废片新生所拯救你的旧照片！', path: '/pages/index/index'};
    }
    return {
      title: '送你 ✦30 光子，来废片新生所拯救你的废旧照片！',
      path: '/pages/index/index?invite=' + this.data.inviteCode,
      imageUrl: '/images/logo.jpg'
    };
  }
});
