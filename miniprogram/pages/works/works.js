const app = getApp();
const api = require('../../utils/api.js');
const creationDraft = require('../../utils/creation-draft.js');
const update=(page,patch)=>typeof api.setDataStable==='function'?api.setDataStable(page,patch):page.setData(patch);
const reusablePreview = url => !!url &&
  (typeof api.isReusableMediaUrl !== 'function' || api.isReusableMediaUrl(url));

Page({
  data: {
    works: [], filteredWorks: [], activeStatus: 'all', loading: false, loaded: false, loadError: '',
    loadingMore:false,hasMore:false,total:0,processingCount:0,focusJobId:'',savingJobId:'',
    workFilters: [{id:'all',name:'全部',count:0},{id:'processing',name:'生成中',count:0},
      {id:'succeeded',name:'已完成',count:0},{id:'failed',name:'失败',count:0}]
  },

  setWorks(works) {
    const status=w=>w.status || (w.result ? 'succeeded' : 'processing');
    works=works.filter(w=>!w.jobId||!this._deletedIds||!this._deletedIds.has(w.jobId));
    update(this,{works,
      filteredWorks:works.map((w,index)=>Object.assign({},w,{sourceIndex:index})).filter(w=>this.data.activeStatus==='all'||status(w)===this.data.activeStatus),
      workFilters:this.data.workFilters.map(f=>Object.assign({},f,{count:this._statusCounts&&Number.isInteger(this._statusCounts[f.id])?this._statusCounts[f.id]:(f.id==='all'?works.length:works.filter(w=>status(w)===f.id).length)}))});
  },

  onFilterStatus(e) {
    const id=e.currentTarget.dataset.id;
    if(!this.data.workFilters.some(f=>f.id===id))return;
    if(id===this.data.activeStatus)return;
    if(typeof api.request!=='function'){update(this,{activeStatus:id});this.setWorks(this.data.works);return;}
    this.clearPendingRefresh();this._loadVersion=(this._loadVersion||0)+1;
    update(this,{activeStatus:id,hasMore:false,focusJobId:'',loaded:false});
    this.setWorks([]);
    return this.loadWorks(true).finally(()=>this.schedulePendingRefresh());
  },

  onRetryWorks() { return this.loadWorks(true).finally(() => this.schedulePendingRefresh()); },

  onLoad(options={}) {
    if(this.data.workFilters.some(f=>f.id===options.status))update(this,{activeStatus:options.status});
    this._requestedJob=options.jobId||'';
  },

  onShow() {
    this._visible = true;
    this._pollDeadline = Date.now() + 30 * 60 * 1000;
    this._pollStartedAt = Date.now();
    return this.loadWorks().then(()=>this.locateRequestedWork()).finally(() => this.schedulePendingRefresh());
  },

  onHide() {
    this._visible = false;
    this._loadVersion = (this._loadVersion || 0) + 1;
    update(this,{loading:false,loadingMore:false});
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
        (!this.data.processingCount && !this.data.works.some(w => w.status === 'processing'))) return;
    this._pendingTimer = setTimeout(async () => {
      this._pendingTimer = null;
      if (!this._visible) return;
      await this.refreshPendingWorks();
      this.schedulePendingRefresh();
    }, Date.now() - this._pollStartedAt > 60000 ? 5000 : api.POLL_INTERVAL || 1500);
  },

  async onPullDownRefresh() {
    await this.loadWorks(true);
    wx.stopPullDownRefresh();
    this.schedulePendingRefresh();
  },

  async loadWorks(force = false, more = false) {
    more=more===true;
    if(more&&(this.data.loading||this.data.loadingMore||!this.data.hasMore))return;
    if(!more&&!force&&this._lastLoadedAt&&Date.now()-this._lastLoadedAt<30000&&
      this._loadedStatus===this.data.activeStatus&&!this.data.loadError)return;
    const version = (this._loadVersion || 0) + 1;
    this._loadVersion = version;
    const filter=this.data.activeStatus,offset=more?(this._nextOffset||0):0;
    const startingJobIds=new Set((app.globalData.historyList||[]).map(w=>w.jobId).filter(Boolean));
    update(this,{loading:!more,loadingMore:more,loadError:''});
    if(!more&&!this.data.loaded&&!this.data.works.length)this.setWorks((app.globalData.historyList||[])
      .filter(w=>filter==='all'||w.status===filter).slice(0,24).map(w=>({...w,preview:this.safePreview(w)})));
    try {
      const res=await this.requestWorks(filter,offset);
      if (this._loadVersion !== version) return;
      if(!res||!Array.isArray(res.jobs))throw new Error('作品列表数据异常');
      if(res.has_more&&(!Number.isInteger(Number(res.next_offset))||Number(res.next_offset)<=offset))throw new Error('作品分页异常');
      const old=new Map((app.globalData.historyList||[]).map(w=>[w.jobId,w]));
      const cloud=res.jobs.filter(cj=>!this._deletedIds||!this._deletedIds.has(cj.id)).map(cj=>this.mapCloudWork(cj,old.get(cj.id)));
      const cloudIds=new Set(cloud.map(w=>w.jobId));
      // An earlier list snapshot must not erase a task accepted while this request was in flight.
      const newlyAccepted=!more?(app.globalData.historyList||[]).filter(w=>w.jobId&&!startingJobIds.has(w.jobId)&&!cloudIds.has(w.jobId)&&w.status==='processing'):[];
      const visibleNew=(filter==='all'||filter==='processing')?newlyAccepted:[];
      const localOnly=filter==='all'&&!more?(app.globalData.historyList||[]).filter(w=>!w.jobId).map(w=>({...w,preview:this.safePreview(w)})):[];
      const merged=(more?this.data.works.filter(w=>!w.jobId):localOnly).concat([...new Map((more?this.data.works.filter(w=>w.jobId).concat(cloud):visibleNew.concat(cloud)).map(w=>[w.jobId,w])).values()]);
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
      if(!more&&!res.has_more){
        const acceptedIds=new Set(newlyAccepted.map(w=>w.jobId));
        app.globalData.historyList=(app.globalData.historyList||[]).filter(w=>!w.jobId||(filter!=='all'&&w.status!==filter)||cloudIds.has(w.jobId)||acceptedIds.has(w.jobId));
      }
      this.mergeHistory(cloud);
      this.applySummary(res);
      this._nextOffset=Number(res.next_offset)||0;
      update(this,{hasMore:!!res.has_more,total:Number.isInteger(res.total)?res.total:merged.length,loaded:true});
      this.setWorks(merged);
      this._lastLoadedAt = Date.now();this._loadedStatus=filter;
    } catch (e) {
      if(this._loadVersion===version)update(this,{loadError:'作品加载失败，请检查网络后重试'});
    }
    if(this._loadVersion===version)update(this,{loading:false,loadingMore:false});
  },

  requestWorks(status='all',offset=0){
    if(typeof api.myJobs==='function')return api.myJobs(24,offset,status);
    if(typeof api.request==='function')return api.request('/api/my/jobs?limit=24&offset='+offset+'&status='+encodeURIComponent(status));
    return api.myJobs(24,offset);
  },
  applySummary(res){
    if(res.status_counts){this._statusCounts={...res.status_counts};if(!Number.isInteger(this._statusCounts.all))this._statusCounts.all=['processing','succeeded','failed'].reduce((n,k)=>n+(Number(this._statusCounts[k])||0),0);}
    if(Number.isInteger(res.processing_count))update(this,{processingCount:res.processing_count});
  },
  mapCloudWork(cj,old={}){
    const image=(url,prior)=>typeof api.stableImageUrl==='function'?api.stableImageUrl(url||'',prior):api.absolute(url||'');
    const work={...old,jobId:cj.id,original:image(cj.orig_url,old.original),result:cj.status==='succeeded'?image(cj.result_url,old.result):'',
      thumbnail:cj.status==='succeeded'?image(cj.thumb_url,old.thumbnail):'',
      status:cj.status,stage:cj.stage,error:cj.error||'',violation:cj.violation||null,quality:cj.quality,provider:cj.provider,
      width:cj.width,height:cj.height,templateId:cj.template_id||'',templateName:cj.template_name||'',createdAt:cj.created_at||old.createdAt||0,
      completedAt:cj.completed_at||0,expiresAt:cj.expires_at||0,chargedAmount:cj.charged_amount,refundedAmount:cj.refunded_amount,settlement:cj.settlement,
      time:cj.created_at?this.formatTime(new Date(cj.created_at*1000)):old.time||'近期'};
    work.stageLabel=({queued:'排队中',normalize:'准备照片中',enhance:'生成图片中',upscale:'精细处理中',store_cos:'保存图片中',finalize:'整理结果中'})[cj.stage]||'生成中';
    const remaining=work.expiresAt*1000-Date.now();
    work.expiryLabel=work.status==='succeeded'&&work.expiresAt&&remaining<=3*86400000?(remaining>0?'还剩 '+Math.max(1,Math.ceil(remaining/86400000))+' 天，请及时保存':'云端图片已到期'):'';
    work.settlementLabel=this.settlementLabel(work);
    const pinned=old.status==='succeeded'&&reusablePreview(old.preview)?this.safePreview(old):'';
    const previewUrl=cj.thumb_url||cj.result_url;
    work.preview=previewUrl&&(typeof api.isJobCosUrl!=='function'||api.isJobCosUrl(previewUrl))?
      (typeof api.jobPreviewUrl==='function'?api.jobPreviewUrl(cj,old.preview):
        (typeof api.stableImageUrl==='function'?image(previewUrl,old.preview):(pinned||this.safePreview(work)))):'';
    return work;
  },
  settlementLabel(work){
    const s=work.settlement||{},charged=work.chargedAmount!=null?work.chargedAmount:s.charged_amount,
      refunded=work.refundedAmount!=null?work.refundedAmount:s.refunded_amount;
    if(Number.isInteger(charged)&&Number.isInteger(refunded))return '扣除 '+charged+' · 已退回 '+refunded+' 光子';
    return work.status==='failed'?'扣退明细请到光子中心核对':'';
  },
  mergeHistory(incoming){
    const all=(app.globalData.historyList||[]).concat(incoming).filter(w=>!w.jobId||!this._deletedIds||!this._deletedIds.has(w.jobId));
    app.globalData.historyList=all.filter(w=>!w.jobId).concat([...new Map(all.filter(w=>w.jobId).map(w=>[w.jobId,w])).values()]).sort((a,b)=>(b.createdAt||0)-(a.createdAt||0));app.persist();
  },
  onReachBottom(){return this.loadWorks(true,true);},
  onMoreWorks(){return this.loadWorks(true,true);},
  async locateRequestedWork(){
    const id=this._requestedJob;if(!id)return;this._requestedJob='';
    try{
      let item=this.data.works.find(w=>w.jobId===id);
      if(!item){const job=await api.request('/api/jobs/'+encodeURIComponent(id));if(!this._visible)return;item=this.mapCloudWork(job);this.setWorks([item,...this.data.works.filter(w=>w.jobId!==id)]);this.mergeHistory([item]);}
      update(this,{focusJobId:id});if(wx.pageScrollTo)wx.pageScrollTo({selector:'#work-'+id,duration:250});
    }catch(e){wx.showModal({title:'作品记录',content:e.message||'作品不存在或已到期，光子流水仍可核对',showCancel:false});}
  },

  safePreview(work) {
    if(work.thumbnail&&reusablePreview(work.thumbnail))return typeof api.communityImage==='function'?api.communityImage(work.thumbnail):work.thumbnail;
    let local = work.jobId && app.globalData.mediaCache && app.globalData.mediaCache[work.jobId] &&
      app.globalData.mediaCache[work.jobId].result;
    if(local&&typeof api.isLocalImageAvailable==='function'&&!api.isLocalImageAvailable(local))local='';
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
    if(item.thumbnail&&typeof api.forgetCommunityImage==='function')api.forgetCommunityImage(item.thumbnail);
    if (!item.thumbnail && app.globalData.mediaCache && app.globalData.mediaCache[item.jobId])
      delete app.globalData.mediaCache[item.jobId].result;
    this._previewRepairAttempts = this._previewRepairAttempts || new Set();
    if (this._previewRepairAttempts.has(item.jobId)) return;
    this._previewRepairAttempts.add(item.jobId);
    api.repairJobMedia(item.jobId, 'result').then(url => {
      const works = this.data.works.map(w => w.jobId === item.jobId ?
        Object.assign({}, w, {result:url, thumbnail:'', preview:url, error:''}) : w);
      this.setWorks(works);
    }).catch(err => {
      const works = this.data.works.map(w => w.jobId === item.jobId ?
        Object.assign({}, w, {result:'', thumbnail:'', preview:'', error:err.message || '结果图暂不可用'}) : w);
      this.setWorks(works);
    });
  },
  onWorkImageLoad(e) {
    const w=this.data.works[e.currentTarget.dataset.index];
    if(w&&typeof api.rememberCommunityImage==='function')api.rememberCommunityImage(w.preview).catch(()=>{});
  },

  async refreshPendingWorks() {
    const version=this._loadVersion,visiblePending=this.data.works.filter(w=>w.jobId&&w.status==='processing');
    try{
      const res=await this.requestWorks('processing',0);
      if(!this._visible||version!==this._loadVersion)return;
      const prior=new Map((app.globalData.historyList||[]).map(w=>[w.jobId,w]));
      const updates=(res.jobs||[]).map(j=>this.mapCloudWork(j,prior.get(j.id)));
      const pendingIds=new Set(updates.map(w=>w.jobId));
      const missing=visiblePending.filter(w=>!pendingIds.has(w.jobId));
      const start=missing.length?(this._pendingDetailOffset||0)%missing.length:0;
      const disappeared=missing.slice(start).concat(missing.slice(0,start)).slice(0,6);
      this._pendingDetailOffset=missing.length?(start+disappeared.length)%missing.length:0;
      const completed=await Promise.all(disappeared.map(async w=>{
        try{return this.mapCloudWork(await api.request('/api/jobs/'+encodeURIComponent(w.jobId)),w);}catch(e){return null;}
      }));
      if(!this._visible||version!==this._loadVersion)return;
      completed.filter(Boolean).forEach(w=>updates.push(w));
      const byId=new Map(updates.map(w=>[w.jobId,w]));
      this.mergeHistory(updates);this.applySummary(res);
      this.setWorks(this.data.works.map(w=>byId.get(w.jobId)||w));
      if(this._statusCounts)update(this,{total:this._statusCounts[this.data.activeStatus]||0});
    }catch(e){/* Keep the last known status; explicit refresh exposes network errors. */}
  },

  /**
   * 刷新单件作品的直链：向服务端用 jobId 换取新鲜签名或最新状态
   */
  refreshWork(item) {
    if (!item || !item.jobId) return Promise.resolve(item);
    return api.request('/api/jobs/' + item.jobId)
      .then(async (job) => {
        if (!job) return item;
        if(this._deletedIds&&this._deletedIds.has(item.jobId))return null;
        const fresh = this.mapCloudWork(job,item);
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
        } else if (job.status === 'failed') {
          fresh.status = 'failed';
          fresh.error = job.error || '生成失败';fresh.violation=job.violation||null;
        }
        this.mergeHistory([fresh]);
        this.setWorks(this.data.works.map(w=>w.jobId===fresh.jobId?fresh:w));
        return fresh;
      })
      .catch(() => item);
  },

  onTapWork(e) {
    this.onMoreWork(e);
  },

  onMoreWork(e) {
    const index=Number(e.currentTarget.dataset.index),item=this.data.works[index];
    if(!item)return;
    if (item.status === 'failed') {
      wx.showActionSheet({
        itemList: ['查看失败原因', '删除此件作品'].concat(item.violation?['提交误判反馈']:[]),
        success: (r) => {
          if (r.tapIndex === 1) this.deleteSingleWork(index,item);
          else if(r.tapIndex===2)this.submitWorkFeedback(item);
           else wx.showModal({title: '生成未完成', content: (item.error || '生成失败')+'\n'+this.settlementLabel(item), showCancel: false});
        }
      });
      return;
    }

    if (item.status === 'processing' || !item.result) {
      wx.showActionSheet({itemList:['查看最新进度','删除此件作品'],itemColor:'#1a1917',success:r=>{
        if(r.tapIndex===1){this.deleteSingleWork(index,item);return;}
        if(r.tapIndex!==0)return;
        wx.showLoading({title:'检查最新进度…'});
        this.refreshWork(item).then(fresh=>{
          if(fresh&&fresh.status==='failed')wx.showModal({title:'生成未完成',content:(fresh.error||'生成失败')+'\n'+this.settlementLabel(fresh),showCancel:false});
          else if(fresh&&fresh.status==='succeeded'&&!fresh.result)wx.showModal({title:'图片暂不可用',content:fresh.error||'请稍后再试',showCancel:false});
          else wx.showToast({title:fresh&&fresh.status==='succeeded'?'已完成，请从更多操作中查看':'仍在云端处理中，请稍后刷新',icon:'none'});
        }).finally(()=>wx.hideLoading());
      }});
      return;
    }
    this.showWorkActions(item,index);
  },

  openWorkPreview(item) {
    if(!item||!item.result||item.status&&item.status!=='succeeded')return;
    if(!item.jobId){
      const local=/^(wxfile:|https?:\/\/(tmp|usr)\/)/i.test(item.result);
      if(!local&&typeof api.isJobCosUrl==='function'&&!api.isJobCosUrl(item.result)&&/^https?:|^\/api\//i.test(item.result)){
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
    if(!item||!item.result||item.status&&item.status!=='succeeded')return;
    const actions=[
      {label:'全屏高清查看',run:()=>this.openWorkPreview(item)},
      {label:'对比原图模式',run:()=>{
        if(!item.jobId){wx.showModal({title:'图片暂不可用',content:'旧记录没有任务编号，无法刷新 COS 图片。',showCancel:false});return;}
        wx.navigateTo({url:`/pages/compare/compare?result=${encodeURIComponent(item.result)}&original=${encodeURIComponent(item.original||'')}&job=${encodeURIComponent(item.jobId)}`});
      }},
      {label:'删除此件作品',run:()=>this.deleteSingleWork(index,item)},
      {label:'沿用参数再创作',run:()=>this.recreateWork(item)},
      {label:'保存到相册',run:()=>this.saveWork(item)}
    ];
    wx.showActionSheet({
      itemList: actions.map(action=>action.label),
      itemColor: '#1a1917',
      success:res=>{const action=actions[res.tapIndex];if(action)action.run();}
    });
  },

  openSubmission(item) {
    if(!item||!item.jobId||item.status!=='succeeded'){wx.showToast({title:'请选择已完成的作品',icon:'none'});return;}
    wx.navigateTo({url:'/pages/community-submit/community-submit?job='+encodeURIComponent(item.jobId)});
  },
  async recreateWork(item){
    if(!item||!item.jobId||this._recreating)return;this._recreating=true;
    try{await creationDraft.recreateFromJob(item.jobId);}
    catch(e){wx.showToast({title:e.message||'参数读取失败，请重试',icon:'none'});}
    finally{this._recreating=false;}
  },
  onSaveWork(e){return this.saveWork(this.data.works[e.currentTarget.dataset.index]);},
  async saveWork(item){
    if(!item||!item.result||item.status&&item.status!=='succeeded'||this.data.savingJobId)return;
    const local=/^(wxfile:|https?:\/\/(tmp|usr)\/)/i.test(item.result);
    if(!item.jobId&&!local&&(/^\/api\//.test(item.result)||/^https?:\/\//i.test(item.result)&&typeof api.isJobCosUrl==='function'&&!api.isJobCosUrl(item.result))){wx.showModal({title:'图片暂不可用',content:'旧记录的图片链接已失效，请查看其他作品。',showCancel:false});return;}
    update(this,{savingJobId:item.jobId||'local'});wx.showLoading({title:'正在保存照片…'});
    try{
      const path=item.jobId?await api.downloadJobMedia(item.jobId,'result',item.result):!local&&/^https?:\/\//i.test(item.result)?await new Promise((resolve,reject)=>wx.getImageInfo({src:item.result,success:r=>resolve(r.path),fail:reject})):item.result;
      await new Promise((resolve,reject)=>wx.saveImageToPhotosAlbum({filePath:path,success:resolve,fail:reject}));
      wx.showToast({title:'已保存到相册',icon:'success'});
    }catch(e){
      if(/auth|deny|denied/i.test(e.errMsg||''))wx.showModal({title:'开启相册权限',content:'允许保存到相册后，可再点击保存。',confirmText:'去设置',success:r=>{if(r.confirm)wx.openSetting({});}});
      else wx.showToast({title:e.message||'保存未完成，请重试',icon:'none'});
    }finally{wx.hideLoading();update(this,{savingJobId:''});}
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
          this.setWorks(this.data.works.filter(w=>w!==item&&(!item.jobId||w.jobId!==item.jobId)));
          await this.loadWorks(true);
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
          await this.loadWorks(true);
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
