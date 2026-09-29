/**
 * 全局状态 - 废片新生所
 *
 * apiBase 通过 ext.json / 编译配置注入更佳，这里保留默认值 + 运行时可改。
 * 真机调试时必须改成局域网 IP 或已备案域名，127.0.0.1 在手机上指向手机自己。
 */
const DEFAULT_API_BASE = 'http://127.0.0.1:8000';
const STORAGE_TOKENS = 'fishTokens';
const STORAGE_HISTORY = 'historyList';
const STORAGE_USER_ID = 'studioUserId';
const STORAGE_TASK_DATE = 'videoTaskDate';
const STORAGE_TASK_COUNT = 'videoTaskCount';

App({
  globalData: {
    apiBase: DEFAULT_API_BASE,
    fishTokens: 0,
    historyList: [],
    welcomeGranted: false,
    selectedTemplate: null, // 从模板库或社区携带过来的目标模板
    userId: '',
    videoTasksToday: 0,
    maxVideoTasks: 3
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

    // 初始积分
    const saved = wx.getStorageSync(STORAGE_TOKENS);
    if (typeof saved === 'number' && saved >= 0) {
      this.globalData.fishTokens = saved;
    } else {
      // 首次启动赠送 90 算力（与参考图一致）
      this.globalData.fishTokens = 90;
      this.globalData.welcomeGranted = true;
      wx.setStorageSync(STORAGE_TOKENS, 90);
    }

    // 历史作品
    const history = wx.getStorageSync(STORAGE_HISTORY);
    if (Array.isArray(history)) this.globalData.historyList = history;

    // 用户唯一 ID
    let uid = wx.getStorageSync(STORAGE_USER_ID);
    if (!uid) {
      const randStr = Math.random().toString(36).substring(2, 8).toUpperCase() + 
                      Math.random().toString(36).substring(2, 6).toUpperCase();
      uid = 'PX-' + randStr;
      wx.setStorageSync(STORAGE_USER_ID, uid);
    }
    this.globalData.userId = uid;

    // 今日看视频任务计数
    const today = new Date().toISOString().slice(0, 10);
    const lastDate = wx.getStorageSync(STORAGE_TASK_DATE);
    if (lastDate !== today) {
      this.globalData.videoTasksToday = 0;
      wx.setStorageSync(STORAGE_TASK_DATE, today);
      wx.setStorageSync(STORAGE_TASK_COUNT, 0);
    } else {
      this.globalData.videoTasksToday = Number(wx.getStorageSync(STORAGE_TASK_COUNT)) || 0;
    }
  },

  onHide() {
    this.persist();
  },

  persist() {
    try {
      wx.setStorageSync(STORAGE_TOKENS, this.globalData.fishTokens);
      wx.setStorageSync(STORAGE_HISTORY, this.globalData.historyList || []);
      wx.setStorageSync(STORAGE_TASK_COUNT, this.globalData.videoTasksToday);
    } catch (e) {
      console.warn('本地存储写入失败', e);
    }
  },

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

  recordVideoTask() {
    if (this.globalData.videoTasksToday < this.globalData.maxVideoTasks) {
      this.globalData.videoTasksToday += 1;
      this.addToken(10);
      return true;
    }
    return false;
  },

  clearHistory() {
    this.globalData.historyList = [];
    this.persist();
  }
});