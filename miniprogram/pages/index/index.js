const app = getApp();
const api = require('../../utils/api.js');
const creationDraft = require('../../utils/creation-draft.js');
const update=(page,patch)=>typeof api.setDataStable==='function'?api.setDataStable(page,patch):page.setData(patch);
const TASK_STAGES = {queued:'排队中',prepare:'准备照片',normalize:'准备照片',audit_input:'原图审核',input_audit:'原图审核',submit:'提交生成',enhance:'AI 生成中',provider_wait:'AI 生成中',import_wait:'保存成品',finalize:'处理成品',audit_output:'成品审核',output_audit:'成品审核',store_cos:'保存成品'};

Page({
  onOpenTextGeneration() {wx.navigateTo({url:'/pages/text-generation/text-generation'});},
  data: {
    lightPoints: 0,
    freeMode: false,
    priceLight: 40,
    priceFine: 40,
    notice: null,
    featuredTemplates: [],
    templateError: '',
    activeTemplate: null,
    historyCount: 0, pendingCount: 0, pendingSummary: '', taskError: '', finishedJobId: '', finishedText: '', draftSummary: ''
  },

  onLoad(options) {
    // 分享路径携带的邀请码：先存本地，登录成功后绑定（服务端发奖，仅一次）
    if (options && options.invite) {
      try { wx.setStorageSync('pendingInvite', options.invite); } catch (e) {}
    }
    this.loadNotice();
    this.bindPendingInvite();
  },

  onShow() {
    this._visible=true;
    this.loadConfig();
    this.loadTemplates();
    const draft=typeof creationDraft.readDraft==='function'?creationDraft.readDraft():null;
    update(this,{draftSummary:draft ? (draft.input_mode==='text'?'继续上次文字创作':draft.imageMissing?'继续草稿 · 请重新选择照片':'继续上次照片创作') : ''});
    this.loadPendingTasks();
    update(this,{
      lightPoints: app.globalData.lightPoints,
      freeMode: app.globalData.freeMode,
      historyCount: (app.globalData.historyList || []).length
    });

    // 检查是否有跨页面携带过来的模板（模板库/社区点了「做同款」）
    if (app.globalData.selectedTemplate) {
      const tpl = app.globalData.selectedTemplate;
      app.globalData.selectedTemplate = null;
      update(this,{ activeTemplate: tpl });
    }
  },

  onHide() {this._visible=false;this._taskVersion=(this._taskVersion||0)+1;if(this._taskTimer)clearTimeout(this._taskTimer);this._taskTimer=null;},
  onUnload() {this._unloaded=true;this.onHide();},
  async loadPendingTasks() {
    if(!this._visible || this._taskLoading || typeof api.myJobs!=='function')return;
    this._taskLoading=true;const version=(this._taskVersion||0)+1;this._taskVersion=version;
    try{
      const response=await api.myJobs(3,0,'processing');
      if(!this._visible||this._taskVersion!==version)return;
      const pending=(response&&response.jobs)||[];
      const old=(app.globalData.historyList||[]).filter(w=>w.jobId&&w.status==='processing');
      const active=new Set(pending.map(j=>j.id));
      const gone=old.filter(w=>!active.has(w.jobId)).slice(0,3);
      for(const w of gone){
        try{
          const job=await api.request('/api/jobs/'+encodeURIComponent(w.jobId));
          if(!this._visible||this._taskVersion!==version)return;
          if(job.status==='succeeded'||job.status==='failed'){
            const index=(app.globalData.historyList||[]).findIndex(x=>x.jobId===w.jobId);
            if(index>=0)Object.assign(app.globalData.historyList[index],{status:job.status,result:job.result_url?api.absolute(job.result_url):'',error:job.error||''});
            update(this,{finishedJobId:w.jobId,finishedText:job.status==='succeeded'?'新作品已完成 · 点击查看':'有作品生成失败 · 点击查看结算'});
          }
        }catch(e){if(e.status===404){const found=(app.globalData.historyList||[]).find(x=>x.jobId===w.jobId);if(found)found.status='expired';}}
      }
      if(!this._visible||this._taskVersion!==version)return;
      const first=pending[0],elapsed=first?Math.max(0,Math.floor(Date.now()/1000-(first.created_at||Date.now()/1000))):0;
      const count=Number(response.processing_count)||Number(response.total)||pending.length;
      update(this,{pendingCount:count,pendingSummary:first?(TASK_STAGES[first.stage]||'云端处理中')+' · 已等待 '+(elapsed<60?elapsed+' 秒':Math.floor(elapsed/60)+' 分钟'):'',taskError:''});
      const history=app.globalData.historyList||[];
      pending.forEach(j=>{const w=history.find(x=>x.jobId===j.id);if(w)Object.assign(w,{status:j.status,stage:j.stage});else history.unshift({jobId:j.id,status:j.status,stage:j.stage,templateName:j.template_name,createdAt:j.created_at});});
      app.globalData.historyList=history;app.persist();
    }catch(e){if(this._visible&&this._taskVersion===version)update(this,{taskError:'任务状态暂未更新，点击重试'});}
    finally{this._taskLoading=false;if(this._visible){if(this._taskTimer)clearTimeout(this._taskTimer);if(this._taskVersion!==version)this._taskTimer=setTimeout(()=>this.loadPendingTasks(),0);else if(this.data.pendingCount)this._taskTimer=setTimeout(()=>this.loadPendingTasks(),5000);}}
  },
  onOpenPendingTasks(){wx.navigateTo({url:'/pages/works/works?status=processing'});},
  onOpenFinishedTask(){const id=this.data.finishedJobId;update(this,{finishedText:''});wx.navigateTo({url:'/pages/works/works?jobId='+encodeURIComponent(id)});},
  onResumeDraft(){if(typeof creationDraft.resumeDraft==='function')creationDraft.resumeDraft().catch(e=>wx.showToast({title:e.message||'草稿暂未恢复',icon:'none'}));},

  /** 登录后绑定邀请码，奖励由服务端配置与发放。 */
  bindPendingInvite() {
    let code = '';
    try { code = wx.getStorageSync('pendingInvite') || ''; } catch (e) {}
    if (!code) return;
    api.ensureLogin()
      .then(() => api.bindInvite(code))
      .then((d) => {
        try { wx.removeStorageSync('pendingInvite'); } catch (e) {}
        if (d && typeof d.balance === 'number') app.setBalance(d.balance);
        update(this,{ lightPoints: app.globalData.lightPoints });
        wx.showToast({ title: `邀请奖励 ✦${typeof d.reward === 'number' ? d.reward : 40} 已到账`, icon: 'none' });
      })
      .catch((err) => {
        try { wx.removeStorageSync('pendingInvite'); } catch (e) {}
        console.warn('邀请码绑定失败', err);
      });
  },

  onPullDownRefresh() {
    return this.loadTemplates(true).finally(()=>wx.stopPullDownRefresh());
  },

  loadTemplates(force = false) {
    if(this._loadingTemplates)return this._loadingTemplates;
    // Only a recent, catalog-checked snapshot may paint the home gallery.
    try {
      const cached=wx.getStorageSync('cached_templates_items'),meta=wx.getStorageSync('home_template_catalog_meta');
      if(Array.isArray(cached)&&meta&&Array.isArray(meta.groups)&&Date.now()-meta.at>=0&&Date.now()-meta.at<30000){
        this._lastTemplates=cached;this._templateGroups=meta.groups;this._renderFeatured(cached);
      }
    } catch (e) {}
    if(!force&&Number.isFinite(this._lastTemplatesAt)&&Date.now()-this._lastTemplatesAt>=0&&Date.now()-this._lastTemplatesAt<30000){
      if(this._lastTemplates)this._renderFeatured(this._lastTemplates);
      return Promise.resolve();
    }
    const task=api.templates().then(d=>{
      if(this._unloaded)return;
      if(!d||!Array.isArray(d.items))throw new Error('模板列表数据异常');
      this._lastTemplates=d.items;this._templateGroups=Array.isArray(d.groups)?d.groups:null;
      this._lastTemplatesAt=Date.now();
      try {
        wx.setStorageSync('cached_templates_items',d.items);
        wx.setStorageSync('home_template_catalog_meta',{at:this._lastTemplatesAt,groups:this._templateGroups});
      }catch(e){}
      if(this._visible!==false){this._renderFeatured(d.items);update(this,{templateError:''});}
    }).catch(()=>{
      if(this._unloaded)return;
      this._lastTemplates=[];this._templateGroups=[];
      if(this._visible!==false){this._renderFeatured([]);update(this,{templateError:'风格暂未加载，请下拉刷新重试'});}
    }).finally(()=>{if(this._loadingTemplates===task)this._loadingTemplates=null;});
    this._loadingTemplates=task;return task;
  },

  _renderFeatured(items) {
    const free = !!this.data.freeMode;
    const pFine = this.data.priceFine != null ? this.data.priceFine : 40;
    const pLight = this.data.priceLight != null ? this.data.priceLight : 40;
    const prior=new Map(this.data.featuredTemplates.map(x=>[x.id,x]));this._featuredSources={};
    const groups=Array.isArray(this._templateGroups)?new Set(this._templateGroups.filter(g=>g.enabled!==false).map(g=>g.id)):null;
    const heat=t=>Number.isFinite(Number(t.usage_count))?Math.max(0,Number(t.usage_count)):0;
    const eligible=(Array.isArray(items)?items:[]).filter(t=>t&&t.id&&t.enabled!==false&&
      (!groups||!t.group_id||t.group_id==='all'||groups.has(t.group_id)));
    const ranked=[...new Map(eligible.map(t=>[t.id,t])).values()].sort((a,b)=>heat(b)-heat(a)||(a.id<b.id?-1:a.id>b.id?1:0));
    const featured = ranked.slice(0, 6).map((t) => {
      const low=Math.min(pLight,pFine),high=Math.max(pLight,pFine);
      let cost = free ? '免扣费' : ('✦ '+(low===high?low:low+'–'+high)+' 光子');
      const old=prior.get(t.id),urls=(t.covers&&t.covers.length?t.covers:[t.cover]).filter(Boolean);
      const preview=t.thumbnail||t.thumbnailUrl||t.cover;
      this._featuredSources[t.id]={url:preview,version:t.cover_version};
      const covers=urls.map((url,i)=>typeof api.stableImageUrl==='function'?api.stableImageUrl(url,old&&old.covers&&old.covers[i],t.cover_version,old&&old.cover_version):api.absolute(url));
      const coverUrl=(typeof api.stableImageUrl==='function'?api.stableImageUrl(preview,old&&old.coverUrl,t.cover_version,old&&old.cover_version):api.absolute(preview))||'/images/logo.jpg';
      return Object.assign({}, t, {
        cover:coverUrl,covers,coverUrl,thumbnail:coverUrl,thumbnailUrl:coverUrl,
        costText: cost
      });
    });
    update(this,{ featuredTemplates: featured });
  },
  onFeaturedImageLoad(e) {
    const s=this._featuredSources&&this._featuredSources[e.currentTarget.dataset.id];
    if(s&&typeof api.rememberCommunityImage==='function')api.rememberCommunityImage(s.url,s.version).catch(()=>{});
  },

  onGoToTemplates() {
    wx.switchTab({
      url: '/pages/templates/templates'
    });
  },

  onSelectFeaturedTemplate(e) {
    const tpl = e.currentTarget.dataset.template;
    if (!tpl) return;
    wx.navigateTo({
      url: `/pages/style-detail/style-detail?id=${encodeURIComponent(tpl.id)}`
    });
  },

  /** 清除待使用模板 */
  onClearTemplate() {
    update(this,{ activeTemplate: null });
  },

  loadNotice() {
    api.announcements()
      .then((d) => {
        const items = (d && d.items) || [];
        update(this,{ notice: items.length ? items[0] : null });
      })
      .catch(() => {});
  },

  onTapNotice() {
    const n = this.data.notice;
    if (!n) return;
    wx.showModal({
      title: n.title || '系统公告',
      content: n.body || '',
      showCancel: false,
      confirmText: '我知道了'
    });
  },

  loadConfig() {
    api.config()
      .then((c) => {
        if (!c) return;
        const p = c.prices || {};
        app.globalData.freeMode = !!c.free_mode;
        update(this,{
          priceLight: (p.light != null) ? p.light : this.data.priceLight,
          priceFine: (p.fine != null) ? p.fine : this.data.priceFine,
          freeMode: !!c.free_mode
        });
        if (this._lastTemplates) this._renderFeatured(this._lastTemplates);
      })
      .catch(() => {});
    // 光子余额以服务端为准
    api.me()
      .then((d) => {
        if (d && typeof d.balance === 'number') app.setBalance(d.balance);
        update(this,{ lightPoints: app.globalData.lightPoints });
      })
      .catch(() => {});
  },

  /** 选择照片入口：选完直接跳转至【调整作品】页，待用模板一并带过去 */
  onPickImage() {
    wx.chooseMedia({
      count: 1,
      mediaType: ['image'],
      sourceType: ['album', 'camera'],
      sizeType: ['original', 'compressed'],
      success: (res) => {
        const file = res.tempFiles[0];
        if (!file || !file.tempFilePath) return;
        const pending = this.data.activeTemplate;
        const defaultTid = pending ? pending.id : '';
        if (pending) update(this,{ activeTemplate: null });

        wx.navigateTo({
          url: `/pages/adjust/adjust?image=${encodeURIComponent(file.tempFilePath)}&templateId=${encodeURIComponent(defaultTid)}`
        });
      }
    });
  },

  /** 积分中心页面 */
  onOpenCreditModal() {
    wx.navigateTo({
      url: '/pages/credits/credits'
    });
  },

  /** 我的作品管理页面 */
  onOpenHistory() {
    wx.navigateTo({
      url: '/pages/works/works'
    });
  }
});
