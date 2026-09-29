const app = getApp();
const api = require('../../utils/api.js');

Page({
  data: {
    works: []
  },

  onLoad() {
    this.loadWorks();
  },

  onShow() {
    this.loadWorks();
  },

  async onPullDownRefresh() {
    await this.loadWorks();
    wx.stopPullDownRefresh();
  },

  async loadWorks() {
    const version = (this._loadVersion || 0) + 1;
    this._loadVersion = version;
    this.setData({ works: app.globalData.historyList || [] });
    try {
      const res = await api.myJobs(100);
      if (this._loadVersion !== version) return;
      const list = app.globalData.historyList || [];
      const deleted = this._deletedIds || new Set();
      const cloud = (res && res.jobs) || [];
      const localOnly = list.filter(w => !w.jobId);
      const byId = new Map(list.filter(w => w.jobId && !deleted.has(w.jobId)).map(w => [w.jobId, w]));
      cloud.forEach(cj => {
        if (deleted.has(cj.id)) return;
        const old = byId.get(cj.id) || {};
        byId.set(cj.id, Object.assign({}, old, {
          jobId: cj.id, original: api.absolute(cj.orig_url || ''),
          result: cj.status === 'succeeded' ? api.absolute(cj.result_url || '') : '',
          status: cj.status, error: cj.error || '', quality: cj.quality,
          provider: cj.provider, width: cj.width, height: cj.height,
          templateName: cj.template_name || '', createdAt: cj.created_at || old.createdAt || 0,
          time: cj.created_at ? this.formatTime(new Date(cj.created_at * 1000)) : old.time || '近期'
        }));
      });
      // A successful empty/full cloud response is authoritative for server jobs.
      const cloudIds = new Set(cloud.map(j => j.id));
      const merged = localOnly.concat([...byId.values()].filter(w => cloudIds.has(w.jobId)))
        .sort((a, b) => (b.createdAt || 0) - (a.createdAt || 0));
      app.globalData.historyList = merged;
      app.persist();
      this.setData({works: merged});
    } catch (e) { /* keep local cache for a transient network failure */ }
    if (this._loadVersion === version) await this.refreshPendingWorks();
  },

  async refreshPendingWorks() {
    const list = app.globalData.historyList || [];
    const pendings = list.filter((w) => w && w.jobId && w.status === 'processing');
    if (!pendings.length) return;

    let hasUpdate = false;
    for (const item of pendings) {
      try {
        const fresh = await this.refreshWork(item);
        if (fresh && fresh.status === 'succeeded') hasUpdate = true;
      } catch (err) {}
    }
    if (hasUpdate) {
      this.setData({ works: app.globalData.historyList || [] });
    }
  },

  /**
   * 刷新单件作品的直链：向服务端用 jobId 换取新鲜签名或最新状态
   */
  refreshWork(item) {
    if (!item || !item.jobId) return Promise.resolve(item);
    return api.request('/api/jobs/' + item.jobId)
      .then((job) => {
        if (!job) return item;
        const fresh = Object.assign({}, item);
        let changed = false;
        if (job.status === 'succeeded' && job.result_url) {
          fresh.status = 'succeeded';
          fresh.original = api.absolute(job.orig_url || '');
          fresh.result = api.absolute(job.result_url);
          changed = true;
        } else if (job.status === 'failed') {
          fresh.status = 'failed';
          fresh.error = job.error || '生成失败';
          changed = true;
        }
        if (changed) {
          const list = app.globalData.historyList || [];
          const idx = list.findIndex((w) => w.jobId === item.jobId);
          if (idx >= 0) {
            list[idx] = fresh;
            app.persist();
            this.setData({ works: list });
          }
        }
        return fresh;
      })
      .catch(() => item);
  },

  onTapWork(e) {
    const index = e.currentTarget.dataset.index;
    const item = this.data.works[index];
    if (!item) return;
    if (item.status === 'failed') {
      wx.showActionSheet({
        itemList: ['查看失败原因', '删除此件作品'],
        success: (r) => {
          if (r.tapIndex === 1) this.deleteSingleWork(index);
          else wx.showModal({title: '生成未完成', content: item.error || '生成失败，光子已按扣款流水核对', showCancel: false});
        }
      });
      return;
    }

    // 如果该作品还在后台运算中
    if (item.status === 'processing' || item.status === 'failed' || !item.result) {
      wx.showLoading({ title: '检查最新进度…' });
      this.refreshWork(item).then((fresh) => {
        wx.hideLoading();
        if (fresh && fresh.result && fresh.status === 'succeeded') {
          this.showWorkActions(fresh, index);
        } else if (fresh && fresh.status === 'failed') {
          wx.showModal({
            title: '生成未完成',
            content: fresh.error || '该照片生成未成功，已退回消耗光子',
            showCancel: false
          });
        } else {
          wx.showToast({
            title: 'AI 仍在云端运算中，请稍候下拉刷新~',
            icon: 'none',
            duration: 2500
          });
        }
      });
      return;
    }

    this.refreshWork(item).then(fresh => this.showWorkActions(fresh, index));
  },

  showWorkActions(item, index) {
    wx.showActionSheet({
      itemList: ['全屏高清查看并保存', '对比原图模式', '删除此件作品'],
      itemColor: '#1a1917',
      success: (res) => {
        if (res.tapIndex === 0) {
          wx.previewImage({
            current: item.result,
            urls: [item.result, item.original].filter(Boolean)
          });
        } else if (res.tapIndex === 1) {
          wx.navigateTo({
            url: `/pages/compare/compare?result=${encodeURIComponent(item.result)}&original=${encodeURIComponent(item.original || '')}&job=${encodeURIComponent(item.jobId || '')}`
          });
        } else if (res.tapIndex === 2) {
          this.deleteSingleWork(index);
        }
      }
    });
  },

  deleteSingleWork(index) {
    wx.showModal({
      title: '删除作品',
      content: '确定要从典藏列表中移除这件作品吗？',
      confirmText: '删除',
      confirmColor: '#9e4b3c',
      cancelText: '保留',
      success: async (res) => {
        if (!res.confirm) return;
        const item = this.data.works[index] || (app.globalData.historyList || [])[index];
        if (!item) return;
        this._loadVersion = (this._loadVersion || 0) + 1;
        try {
          if (item.jobId) await api.deleteJob(item.jobId);
          this._deletedIds = this._deletedIds || new Set();
          if (item.jobId) this._deletedIds.add(item.jobId);
          app.globalData.historyList = (app.globalData.historyList || []).filter(w => w !== item && w.jobId !== item.jobId);
          app.persist();
          this.setData({works: app.globalData.historyList});
          await this.loadWorks();
          wx.showToast({title: '已删除作品', icon: 'success'});
        } catch (err) { wx.showToast({title: err.message || '删除失败，请重试', icon: 'none'}); }
      }
    });
  },

  onClearAllWorks() {
    wx.showModal({
      title: '清空作品集', content: '确定删除全部云端作品和本地记录吗？',
      confirmText: '清空全部', confirmColor: '#9e4b3c', cancelText: '保留',
      success: async (res) => {
        if (!res.confirm) return;
        this._loadVersion = (this._loadVersion || 0) + 1;
        try {
          await api.deleteAllJobs();
          app.clearHistory();
          this.setData({works: []});
          await this.loadWorks();
          wx.showToast({title: '作品集已清空', icon: 'success'});
        } catch (err) { wx.showToast({title: err.message || '清空失败，请重试', icon: 'none'}); }
      }
    });
  },

  onGoCreate() {
    wx.switchTab({
      url: '/pages/index/index'
    });
  },

  formatTime(date) {
    const pad = (n) => (n < 10 ? '0' + n : '' + n);
    return `${date.getFullYear()}.${pad(date.getMonth() + 1)}.${pad(date.getDate())} ${pad(date.getHours())}:${pad(date.getMinutes())}`;
  }
});
