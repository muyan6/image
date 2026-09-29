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
    let list = app.globalData.historyList || [];
    // 自动清洗历史记录中被污染了额外 query 参数的直链，恢复 COS 签名
    let needClean = false;
    list.forEach((item) => {
      if (item && item.result && api.cleanUrl) {
        const cleaned = api.cleanUrl(item.result);
        if (cleaned !== item.result) { item.result = cleaned; needClean = true; }
      }
      if (item && item.original && api.cleanUrl) {
        const cleaned = api.cleanUrl(item.original);
        if (cleaned !== item.original) { item.original = cleaned; needClean = true; }
      }
    });
    if (needClean) app.persist();
    this.setData({ works: list });

    // 1. 同步云端最近任务（支持切屏后自动取回、跨端同步）
    try {
      if (api.myJobs) {
        const res = await api.myJobs(20);
        if (res && res.jobs && res.jobs.length) {
          const existingIds = new Set(list.map((w) => w.jobId));
          let changed = false;
          for (const cj of res.jobs) {
            const orig = api.absolute(cj.orig_url);
            const resUrl = cj.result_url ? api.absolute(cj.result_url) : '';
            if (existingIds.has(cj.id)) {
              const item = list.find((w) => w.jobId === cj.id);
              if (item) {
                if (cj.status === 'succeeded' && resUrl && item.status !== 'succeeded') {
                  item.status = 'succeeded';
                  item.result = resUrl;
                  changed = true;
                } else if (cj.status === 'failed' && item.status !== 'failed') {
                  item.status = 'failed';
                  item.error = cj.error;
                  changed = true;
                }
              }
            } else {
              list.push({
                original: orig,
                result: resUrl,
                status: cj.status,
                quality: cj.quality,
                templateName: cj.template_name || '',
                jobId: cj.id,
                time: cj.created_at ? this.formatTime(new Date(cj.created_at * 1000)) : '近期'
              });
              changed = true;
            }
          }
          if (changed) {
            app.persist();
            this.setData({ works: list });
          }
        }
      }
    } catch (e) {
      // 网络波动静默忽略
    }

    // 2. 检查是否有未完成的任务
    this.refreshPendingWorks();
  },

  async refreshPendingWorks() {
    const list = app.globalData.historyList || [];
    const pendings = list.filter((w) => w && w.jobId && (w.status === 'processing' || !w.result));
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
          fresh.original = api.absolute(job.orig_url || item.original);
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

    // 如果该作品还在后台运算中
    if (item.status === 'processing' || !item.result) {
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

    this.showWorkActions(item, index);
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
      success: (res) => {
        if (res.confirm) {
          app.globalData.historyList.splice(index, 1);
          app.persist();
          this.loadWorks();
          wx.showToast({ title: '已移除作品', icon: 'success' });
        }
      }
    });
  },

  onClearAllWorks() {
    wx.showModal({
      title: '清空作品集',
      content: '确定要清空全部已留存的作品吗？操作后不可恢复。',
      confirmText: '清空全部',
      confirmColor: '#9e4b3c',
      cancelText: '保留',
      success: (res) => {
        if (res.confirm) {
          app.clearHistory();
          this.loadWorks();
          wx.showToast({ title: '作品集已清空', icon: 'success' });
        }
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
