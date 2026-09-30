/**
 * 全局状态 - 废片新生所
 *
 * 光子余额以服务端记账为准（SQLite users.balance），
 * 本地 storage 只作未登录时的展示缓存，每次登录/提交任务后用响应值覆盖。
 *
 * apiBase 通过 ext.json / 编译配置注入更佳，这里保留默认值 + 运行时可改。
 * 真机调试时必须改成局域网 IP 或已备案域名，127.0.0.1 在手机上指向手机自己。
 */
const DEFAULT_API_BASE = 'https://image.myil.top';
const STORAGE_BALANCE = 'lightPoints';
const LEGACY_BALANCE = 'fishTokens'; // 旧版本地积分，仅作一次性迁移展示
const STORAGE_HISTORY = 'historyList';
const STORAGE_USER_ID = 'studioUserId';
const STORAGE_NICKNAME = 'studioNickname';

App({
  globalData: {
    apiBase: DEFAULT_API_BASE,
    lightPoints: 0,
    freeMode: false,
    historyList: [],
    mediaCache: {},
    selectedTemplate: null, // 从模板库或社区携带过来的目标模板
    userId: '',
    nickname: ''
  },

  onLaunch() {
    // 允许通过 ext.json 覆盖后端地址（多环境切换用）
    try {
      if (typeof wx.getExtConfigSync === 'function') {
        const ext = wx.getExtConfigSync() || {};
        if (ext.apiBase) this.globalData.apiBase = ext.apiBase;
      }
    } catch (e) {
      // 没有 ext.json 属于正常情况
    }

    // 光子展示缓存：新版键缺失时读一次旧版本地积分，之后一切以服务端为准
    let saved = wx.getStorageSync(STORAGE_BALANCE);
    if (typeof saved !== 'number' || saved < 0) {
      saved = wx.getStorageSync(LEGACY_BALANCE);
      if (typeof saved !== 'number' || saved < 0) saved = 0;
    }
    this.globalData.lightPoints = saved;

    // 历史作品
    const history = wx.getStorageSync(STORAGE_HISTORY);
    this.globalData.historyList = Array.isArray(history) ? history : [];

    // The display ID comes from the server's OpenID record, never Math.random().
    const uid = wx.getStorageSync(STORAGE_USER_ID);
    this.globalData.userId = typeof uid === 'string' && /^(WX|WEB)-[0-9A-F]{16}$/.test(uid) ? uid : '';
    const nickname = wx.getStorageSync(STORAGE_NICKNAME);
    this.globalData.nickname = typeof nickname === 'string' ? nickname : '';
  },

  onHide() {
    this.persist();
  },

  persist() {
    try {
      wx.setStorageSync(STORAGE_BALANCE, this.globalData.lightPoints);
      wx.setStorageSync(STORAGE_HISTORY, this.globalData.historyList || []);
    } catch (e) {
      console.warn('本地存储写入失败', e);
    }
  },

  /** 用服务端返回的余额覆盖本地展示值 */
  setUserIdentity(uid) {
    if (typeof uid !== 'string' || !/^(WX|WEB)-[0-9A-F]{16}$/.test(uid)) return;
    this.globalData.userId = uid;
    try { wx.setStorageSync(STORAGE_USER_ID, uid); } catch (e) {}
  },

  setNickname(nickname) {
    this.globalData.nickname = typeof nickname === 'string' ? nickname : '';
    try { wx.setStorageSync(STORAGE_NICKNAME, this.globalData.nickname); } catch (e) {}
  },

  setBalance(n) {
    const val = typeof n === 'number' && n >= 0 ? n : 0;
    this.globalData.lightPoints = val;
    this.persist();
    return val;
  },

  clearHistory() {
    this.globalData.historyList = [];
    this.persist();
  }
});
