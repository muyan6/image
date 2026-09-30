const app = getApp();
const api = require('../../utils/api.js');
const reusablePreview = url => !!url &&
  (typeof api.isReusableMediaUrl !== 'function' || api.isReusableMediaUrl(url));

Page({
  data: {
    works: [], filteredWorks: [], activeStatus: 'all', loading: false, loaded: false, loadError: '',
    workFilters: [{id:'all',name:'全部',count:0},{id:'processing',name:'生成中',count:0},
      {id:'succeeded',name:'已完成',count:0},{id:'failed',name:'失败',count:0}]
  },

  setWorks(works) {
    const status=w=>w.status || (w.result ? 'succeeded' : 'processing');
    this.setData({works,
      filteredWorks:works.map((w,index)=>Object.assign({},w,{sourceIndex:index})).filter(w=>this.data.activeStatus==='all'||status(w)===this.data.activeStatus),
      workFilters:this.data.workFilters.map(f=>Object.assign({},f,{count:f.id==='all'?works.length:works.filter(w=>status(w)===f.id).length}))});
  },

  onFilterStatus(e) {
    const id=e.currentTarget.dataset.id;
    if(!this.data.workFilters.some(f=>f.id===id))return;
    this.setData({activeStatus:id});this.setWorks(this.data.works);
  },

  onRetryWorks() { return this.loadWorks(true).finally(() => this.schedulePendingRefresh()); },

  onLoad() {
    // 首次及之后返回本页均由 onShow 加载，避免相同请求并发两次。
  },

  onShow() {
    this._visible = true;
    this._pollDeadline = Date.now() + 30 * 60 * 1000;
    this._pollStartedAt = Date.now();
    return this.loadWorks().finally(() => this.schedulePendingRefresh());
  },

  onHide() {
    this._visible = false;
    this._loadVersion = (this._loadVersion || 0) + 1;
    this.setData({loading:false});
    this.clearPendingRefresh();
  },

  onUnload() {
    this.onHide();
  },

  clearPendingRefresh() {
    if (this._pendingTimer != null) clearTimeout(this._pendingTimer);
    this._pendingTimer = null;
  },

  schedulePendingRefresh() {
    this.clearPendingRefresh();
    if (!this._visible || Date.now() >= this._pollDeadline ||
        !this.data.works.some(w => w.status === 'processing')) return;
    this._pendingTimer = setTimeout(async () => {
      this._pendingTimer = null;
      if (!this._visible) return;
      // 一次列表请求同时更新所有任务，避免 N 个任务串行请求拖长完成展示。
      await this.loadWorks(true);
      this.schedulePendingRefresh();
    }, Date.now() - this._pollStartedAt > 60000 ? 5000 : api.POLL_INTERVAL || 1500);
  },

  async onPullDownRefresh() {
    await this.loadWorks(true);
    wx.stopPullDownRefresh();
    this.schedulePendingRefresh();
  },

  async loadWorks(force = false) {
    const localKeys = (app.globalData.historyList || []).map(w => w.jobId || w.result || '').join('|');
    const shownKeys = this.data.works.map(w => w.jobId || w.result || '').join('|');
    if (!force && this._lastLoadedAt && Date.now() - this._lastLoadedAt < 30000 &&
        localKeys === shownKeys && !this.data.works.some(w => w.status === 'processing') &&
        !this.data.works.some(w => w.preview && !reusablePreview(w.preview))) return;
    const version = (this._loadVersion || 0) + 1;
    this._loadVersion = version;
    const startingJobIds = new Set((app.globalData.historyList || []).map(w => w.jobId).filter(Boolean));
    this.setData({loading:true,loadError:''});
    if (!this.data.works.length) this.setWorks((app.globalData.historyList || []).map(w =>
      Object.assign({}, w, {preview: this.safePreview(w)})));
    try {
      let res = await api.myJobs(100);
      const pages=[...((res&&res.jobs)||[])];let offset=0;
      while(res&&res.has_more){
        if(this._loadVersion!==version)return;
        const next=Number(res.next_offset);if(!Number.isInteger(next)||next<=offset)throw new Error('作品分页异常');
        offset=next;res=await api.myJobs(100,offset);pages.push(...((res&&res.jobs)||[]));
      }
      res={jobs:pages};
      if (this._loadVersion !== version) return;
      const list = app.globalData.historyList || [];
      const deleted = this._deletedIds || new Set();
      const cloud = (res && res.jobs) || [];
      const localOnly = list.filter(w => !w.jobId)
        .map(w => Object.assign({}, w, {preview: this.safePreview(w)}));
      const byId = new Map(list.filter(w => w.jobId && !deleted.has(w.jobId)).map(w => [w.jobId, w]));
      cloud.forEach(cj => {
        if (deleted.has(cj.id)) return;
        const old = byId.get(cj.id) || {};
        const work = Object.assign({}, old, {
          jobId: cj.id, original: api.absolute(cj.orig_url || ''),
          result: cj.status === 'succeeded' ? api.absolute(cj.result_url || '') : '',
          status: cj.status, error: cj.error || '', violation:cj.violation||null, quality: cj.quality,
          provider: cj.provider, width: cj.width, height: cj.height,
          templateName: cj.template_name || '', createdAt: cj.created_at || old.createdAt || 0,
          time: cj.created_at ? this.formatTime(new Date(cj.created_at * 1000)) : old.time || '近期'
        });
        const pinned = old.status === 'succeeded' && reusablePreview(old.preview) ? this.safePreview(old) : '';
        work.preview = pinned || this.safePreview(work);
        byId.set(cj.id, work);
      });
      // A successful empty/full cloud response is authoritative for server jobs.
      const cloudIds = new Set(cloud.map(j => j.id));
      // 在请求启动后，后台上传可能刚被受理；较早的列表快照不应抹掉新任务。
      // 请求开始前已存在的任务仍以云端为准，已删除/到期的旧任务不复活。
      const merged = localOnly.concat([...byId.values()].filter(w => cloudIds.has(w.jobId) ||
        (!startingJobIds.has(w.jobId) && w.status === 'processing')))
        .sort((a, b) => (b.createdAt || 0) - (a.createdAt || 0));
      // 旧任务若只返回后端本地图片路径，先拿 JSON 补存到 COS 再展示前几件作品。
      const visible = merged.slice(0, 6).filter(w => w.jobId && w.status === 'succeeded' &&
        w.result && !this.safePreview(w));
      for (const work of visible) {
        try {
          work.result = await api.repairJobMedia(work.jobId, 'result');
          work.preview = this.safePreview(work);
          work.error = '';
        } catch (err) {
          work.result = '';
          work.preview = '';
          work.error = err.message || '结果图暂不可用';
        }
        if (this._loadVersion !== version) return;
      }
      app.globalData.historyList = merged;
      app.persist();
      this.setWorks(merged);
      this._lastLoadedAt = Date.now();
    } catch (e) {
      if(this._loadVersion===version)this.setData({loadError:'作品加载失败，请检查网络后重试'});
    }
    if(this._loadVersion===version)this.setData({loading:false,loaded:true});
  },

  safePreview(work) {
    const local = work.jobId && app.globalData.mediaCache && app.globalData.mediaCache[work.jobId] &&
      app.globalData.mediaCache[work.jobId].result;
    if (local) return local;
    if (reusablePreview(work.preview) && (/^(wxfile:|http:\/\/tmp\/)/i.test(work.preview) ||
        (typeof api.isJobCosUrl === 'function' && api.isJobCosUrl(work.preview)))) return work.preview;
    if (!work.jobId) {
      const url = work.result || work.original || '';
      if (typeof api.isJobCosUrl === 'function' &&
          (/^https?:\/\//i.test(url) || /^\/api\//.test(url)) && !api.isJobCosUrl(url)) return '';
      return url;
    }
    if (typeof api.isJobCosUrl !== 'function') return work.result || work.original || '';
    return [work.result, work.original].find(url => !!url && api.isJobCosUrl(url) && reusablePreview(url)) || '';
  },

  onWorkImageError(e) {
    const item = this.data.works[e.currentTarget.dataset.index];
    if (!item || !item.jobId || item.status !== 'succeeded') return;
    if (app.globalData.mediaCache && app.globalData.mediaCache[item.jobId])
      delete app.globalData.mediaCache[item.jobId].result;
    this._previewRepairAttempts = this._previewRepairAttempts || new Set();
    if (this._previewRepairAttempts.has(item.jobId)) return;
    this._previewRepairAttempts.add(item.jobId);
    api.repairJobMedia(item.jobId, 'result').then(url => {
      const works = this.data.works.map(w => w.jobId === item.jobId ?
        Object.assign({}, w, {result:url, preview:url, error:''}) : w);
      this.setWorks(works);
    }).catch(err => {
      const works = this.data.works.map(w => w.jobId === item.jobId ?
        Object.assign({}, w, {result:'', preview:'', error:err.message || '结果图暂不可用'}) : w);
      this.setWorks(works);
    });
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
      this.setWorks(app.globalData.historyList || []);
    }
  },

  /**
   * 刷新单件作品的直链：向服务端用 jobId 换取新鲜签名或最新状态
   */
  refreshWork(item) {
    if (!item || !item.jobId) return Promise.resolve(item);
    return api.request('/api/jobs/' + item.jobId)
      .then(async (job) => {
        if (!job) return item;
        const fresh = Object.assign({}, item);
        let changed = false;
        if (job.status === 'succeeded') {
          fresh.status = 'succeeded';
          fresh.original = api.absolute(job.orig_url || '');
          if (job.result_url) {
            const direct = typeof api.isJobCosUrl !== 'function' || api.isJobCosUrl(job.result_url);
            if (direct) fresh.result = api.absolute(job.result_url);
            else {
              try { fresh.result = await api.repairJobMedia(item.jobId, 'result'); }
              catch (err) { fresh.result = ''; fresh.error = err.message || '结果图暂不可用'; }
            }
          } else {
            fresh.result = '';
            fresh.error = '作品图片已到保存期限';
          }
          fresh.preview = this.safePreview(fresh);
          changed = true;
        } else if (job.status === 'failed') {
          fresh.status = 'failed';
          fresh.error = job.error || '生成失败';fresh.violation=job.violation||null;
          changed = true;
        }
        if (changed) {
          const list = app.globalData.historyList || [];
          const idx = list.findIndex((w) => w.jobId === item.jobId);
          if (idx >= 0) {
            list[idx] = fresh;
            app.persist();
            this.setWorks(list);
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
        itemList: ['查看失败原因', '删除此件作品'].concat(item.violation?['提交误判反馈']:[]),
        success: (r) => {
          if (r.tapIndex === 1) this.deleteSingleWork(index,item);
          else if(r.tapIndex===2)this.submitWorkFeedback(item);
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
          this.openWorkPreview(fresh);
        } else if (fresh && fresh.status === 'failed') {
          wx.showModal({
            title: '生成未完成',
            content: fresh.error || '该照片生成未成功，已退回消耗光子',
            showCancel: false
          });
        } else {
          if (fresh && fresh.status === 'succeeded')
            wx.showModal({title:'图片暂不可用',content:fresh.error || '请稍后再试',showCancel:false});
          else wx.showToast({title:'AI 仍在云端运算中，请稍候下拉刷新~',icon:'none',duration:2500});
        }
      });
      return;
    }

    this.refreshWork(item).then(fresh => this.openWorkPreview(fresh));
  },

  onMoreWork(e) {
    const index=Number(e.currentTarget.dataset.index),item=this.data.works[index];
    if(!item)return;
    if(item.status==='failed'){this.onTapWork(e);return;}
    if(item.status==='processing'||!item.result){this.onTapWork(e);return;}
    this.showWorkActions(item,index);
  },

  openWorkPreview(item) {
    if(!item||!item.result)return;
    if(!item.jobId){
      if(typeof api.isJobCosUrl==='function'&&!api.isJobCosUrl(item.result)&&/^https?:|^\/api\//i.test(item.result)){
        wx.showModal({title:'图片暂不可用',content:'旧记录的图片链接已失效，请查看其他作品。',showCancel:false});return;
      }
      wx.previewImage({current:item.result,urls:[item.result]});return;
    }
    if(this._openingPreview)return;
    this._openingPreview=true;wx.showLoading({title:'正在读取作品…'});
    return api.downloadJobMedia(item.jobId,'result',item.result).then(path=>{
      if(this._visible!==false)wx.previewImage({current:path,urls:[path]});
    }).catch(err=>wx.showModal({title:'图片暂不可用',content:err.message||'请稍后重试',showCancel:false}))
      .finally(()=>{this._openingPreview=false;wx.hideLoading();});
  },

  showWorkActions(item, index) {
    wx.showActionSheet({
      itemList: ['全屏高清查看并保存', '对比原图模式', '删除此件作品', '投稿到社区'],
      itemColor: '#1a1917',
      success: (res) => {
        if (res.tapIndex === 0) {
          if (!item.jobId) {
            if (typeof api.isJobCosUrl === 'function' && !api.isJobCosUrl(item.result) &&
                (/^https?:\/\//i.test(item.result || '') || /^\/api\//.test(item.result || ''))) {
              wx.showModal({title:'图片暂不可用',content:'旧记录没有任务编号，无法安全地从 COS 获取原图。',showCancel:false});
              return;
            }
            wx.previewImage({current:item.result, urls:[item.result].filter(Boolean)});
            return;
          }
          wx.showLoading({title:'正在读取作品…'});
          api.downloadJobMedia(item.jobId, 'result', item.result).then(path => {
            wx.previewImage({current:path, urls:[path]});
          }).catch(err => wx.showModal({title:'图片暂不可用',content:err.message || 'COS 下载失败',showCancel:false}))
            .finally(() => wx.hideLoading());
        } else if (res.tapIndex === 1) {
          if (!item.jobId) {
            wx.showModal({title:'图片暂不可用',content:'旧记录没有任务编号，无法刷新 COS 图片。',showCancel:false});
            return;
          }
          wx.navigateTo({
            url: `/pages/compare/compare?result=${encodeURIComponent(item.result)}&original=${encodeURIComponent(item.original || '')}&job=${encodeURIComponent(item.jobId || '')}`
          });
        } else if (res.tapIndex === 2) {
          this.deleteSingleWork(index,item);
        } else if(res.tapIndex===3){this.openSubmission(item);}
      }
    });
  },

  openSubmission(item) {
    if(!item||!item.jobId||item.status!=='succeeded'){wx.showToast({title:'请选择已完成的作品',icon:'none'});return;}
    wx.navigateTo({url:'/pages/community-submit/community-submit?job='+encodeURIComponent(item.jobId)});
  },
  onSubmitWork(e) { this.openSubmission(this.data.works[e.currentTarget.dataset.index]); },
  onMySubmissions() { wx.navigateTo({url:'/pages/community-submit/community-submit'}); },

  submitWorkFeedback(item) {
    const v=item.violation;if(!v)return;
    if(v.feedback_submitted){wx.showToast({title:'反馈已提交，等待复核',icon:'none'});return;}
    wx.showModal({title:'提交误判反馈',editable:true,placeholderText:'请描述需要复核的情况',success:async r=>{
      if(!r.confirm||!(r.content||'').trim())return;
      try{await api.submitViolationFeedback(v.violation_id,r.content.trim());v.feedback_submitted=true;wx.showToast({title:'反馈已提交',icon:'success'});}
      catch(e){wx.showToast({title:e.message||'提交失败',icon:'none'});}}});
  },

  deleteSingleWork(index, selected) {
    const item=selected||this.data.works[index]||(app.globalData.historyList||[])[index];
    if(!item)return;
    wx.showModal({
      title: '删除作品',
      content: '确定要从典藏列表中移除这件作品吗？社区独立副本请在“我的投稿”单独撤回。',
      confirmText: '删除',
      confirmColor: '#9e4b3c',
      cancelText: '保留',
      success: async (res) => {
        if (!res.confirm) return;
        this._loadVersion = (this._loadVersion || 0) + 1;
        try {
          const deletion = item.jobId ? await api.deleteJob(item.jobId) : null;
          this._deletedIds = this._deletedIds || new Set();
          if (item.jobId) this._deletedIds.add(item.jobId);
          if (item.jobId && app.globalData.mediaCache) delete app.globalData.mediaCache[item.jobId];
          const history = app.globalData.historyList || [];
          if (item.jobId) app.globalData.historyList = history.filter(w => w.jobId !== item.jobId);
          else {
            let localIndex = history.indexOf(item);
            if (localIndex < 0) localIndex = history.findIndex(w => !w.jobId &&
              w.result === item.result && w.original === item.original && w.time === item.time &&
              w.timestamp === item.timestamp);
            app.globalData.historyList = history.filter((w, i) => i !== localIndex);
          }
          app.persist();
          this.setWorks(app.globalData.historyList);
          await this.loadWorks();
          wx.showToast({title: deletion && deletion.cos_pending ? '云端清理中' : '已删除作品', icon: 'success'});
        } catch (err) { wx.showToast({title: err.message || '删除失败，请重试', icon: 'none'}); }
      }
    });
  },

  onClearAllWorks() {
    wx.showModal({
      title: '清空作品集', content: '确定删除全部个人云端作品和本地记录吗？社区独立副本请在“我的投稿”单独撤回。',
      confirmText: '清空全部', confirmColor: '#9e4b3c', cancelText: '保留',
      success: async (res) => {
        if (!res.confirm) return;
        this._loadVersion = (this._loadVersion || 0) + 1;
        try {
          await api.deleteAllJobs();
          app.clearHistory();
          app.globalData.mediaCache = {};
          this.setWorks([]);
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
