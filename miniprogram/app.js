/**
 * 全局状态
 *
 * apiBase 通过 ext.json / 编译配置注入更佳，这里保留默认值 + 运行时可改。
 * 真机调试时必须改成局域网 IP 或已备案域名，127.0.0.1 在手机上指向手机自己。
 */
const DEFAULT_API_BASE = 'http://127.0.0.1:8000';
const STORAGE_TOKENS = 'fishTokens';
const STORAGE_HISTORY = 'historyList';

App({
  globalData: {
    apiBase: DEFAULT_API_BASE,
    fishTokens: 0,
    historyList: [],
    welcomeGranted: false
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

    const saved = wx.getStorageSync(STORAGE_TOKENS);
    if (typeof saved === 'number' && saved >= 0) {
      this.globalData.fishTokens = saved;
    } else {
      // 首次启动赠送
      this.globalData.fishTokens = 5;
      this.globalData.welcomeGranted = true;
      wx.setStorageSync(STORAGE_TOKENS, 5);
    }

    const history = wx.getStorageSync(STORAGE_HISTORY);
    if (Array.isArray(history)) this.globalData.historyList = history;
  },

  onHide() {
    this.persist();
  },

  persist() {
    try {
      wx.setStorageSync(STORAGE_TOKENS, this.globalData.fishTokens);
      wx.setStorageSync(STORAGE_HISTORY, this.globalData.historyList || []);
    } catch (e) {
      console.warn('本地存储写入失败', e);
    }
  },

  /**
   * 扣费。返回值表示是否扣成功。
   * 注意：这只是本地演示计费，真正上线必须由后端账本裁决，
   * 否则客户端可以随意改 token 数量。
   */
  consumeToken(num) {
    const cost = typeof num === 'number' ? num : 1;
    const left = this.globalData.fishTokens - cost;
    if (left < 0) return false;
    this.globalData.fishTokens = left;
    this.persist();
    return true;
  },

  addToken(num) {
    const gain = typeof num === 'number' ? num : 1;
    this.globalData.fishTokens += gain;
    this.persist();
    return this.globalData.fishTokens;
  },

  clearHistory() {
    this.globalData.historyList = [];
    this.persist();
  }
});