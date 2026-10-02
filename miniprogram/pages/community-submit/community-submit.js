const api=require('../../utils/api.js');
const app=getApp();
const labels={uploading:'保存中，可继续提交',pending:'等待审核',published:'已发布',rejected:'未通过 / 已下架',withdrawn:'已撤回'};
Page({
  data:{jobId:'',job:null,jobLoading:false,jobError:'',title:'',story:'',categoryIndex:0,
    categories:[{id:'all',name:'按模板自动分组'}],
    showWorkPicker:false,workChoices:[],workPickerLoading:false,workPickerError:'',workPickerHasMore:false,
    shareOriginal:false,consent:false,submitting:false,submitError:'',items:[],loading:false,loadError:'',hasMore:false},
  onLoad(options){if(options&&options.job)this.loadJob(decodeURIComponent(options.job));},
  onShow(){this._visible=true;this.loadMine();},
  onHide(){this._visible=false;},
  onUnload(){this._unloaded=true;this._mineVersion=(this._mineVersion||0)+1;this._jobVersion=(this._jobVersion||0)+1;this._workPickerVersion=(this._workPickerVersion||0)+1;},
  async loadJob(id,preset){
    const version=(this._jobVersion||0)+1;this._jobVersion=version;
    this.setData({jobId:id,job:null,jobLoading:true,jobError:'',shareOriginal:false,consent:false,submitError:''});
    try{
      const job=await api.request('/api/jobs/'+encodeURIComponent(id));
      if(this._unloaded||version!==this._jobVersion)return;
      if(job.status!=='succeeded'||!job.result_url)throw new Error('请选择已完成且未过期的作品');
      this.setData({job:{...job,result_url:api.absolute(job.result_url),orig_url:api.absolute(job.orig_url||'')},
        title:preset?preset.title:(job.template_name||'我的新生作品'),story:preset?preset.story:'',
        categoryIndex:preset?this.categoryIndexFor(preset.category):0,
        shareOriginal:!!(preset&&preset.share_original&&job.orig_url)});
    }catch(e){if(!this._unloaded&&version===this._jobVersion)this.setData({jobError:e.message||'作品读取失败'});}
    finally{if(!this._unloaded&&version===this._jobVersion)this.setData({jobLoading:false});}
  },
  retryJob(){this.loadJob(this.data.jobId);},
  onTitle(e){this.setData({title:e.detail.value});},
  onStory(e){this.setData({story:e.detail.value});},
  onCategory(e){this.setData({categoryIndex:Number(e.detail.value)});},
  onOriginal(e){this.setData({shareOriginal:!!e.detail.value,consent:false});},
  onConsent(e){this.setData({consent:e.detail.value.includes('publish')});},
  categoryIndexFor(id){const index=this.data.categories.findIndex(c=>c.id===id);if(index>=0)return index;if(id&&id!=='all'){this.setData({categories:[...this.data.categories,{id,name:'原分类（保留）'}]});return this.data.categories.length-1;}return 0;},
  onChooseWork(){this.setData({showWorkPicker:true});return this.loadWorkChoices();},
  onCloseWorkPicker(){this._workPickerVersion=(this._workPickerVersion||0)+1;this.setData({showWorkPicker:false,workPickerLoading:false});},
  async loadWorkChoices(more=false){
    more=more===true;if(more&&(this.data.workPickerLoading||!this.data.workPickerHasMore))return;
    const version=(this._workPickerVersion||0)+1;this._workPickerVersion=version;const offset=more?this._workNextOffset||0:0;
    this.setData({workPickerLoading:true,workPickerError:''});
    try{const r=await api.request('/api/my/jobs?limit=24&offset='+offset+'&status=succeeded');if(this._unloaded||version!==this._workPickerVersion||!this.data.showWorkPicker)return;
      const hasMore=!!r.has_more,nextOffset=r.next_offset;
      if(hasMore&&(!Number.isSafeInteger(nextOffset)||nextOffset<=offset))throw new Error('作品分页信息异常，请重试');
      const items=(r.jobs||[]).filter(x=>x.status==='succeeded'&&x.result_url).map(x=>({...x,preview:api.absolute(x.thumb_url||x.result_url),label:x.template_name||'我的创作'}));
      this._workNextOffset=nextOffset;this.setData({workChoices:[...new Map((more?[...this.data.workChoices,...items]:items).map(x=>[x.id,x])).values()],workPickerHasMore:hasMore});
    }catch(e){if(!this._unloaded&&version===this._workPickerVersion)this.setData({workPickerError:e.message||'作品列表读取失败，请重试'});}finally{if(!this._unloaded&&version===this._workPickerVersion)this.setData({workPickerLoading:false});}
  },
  onMoreWorkChoices(){return this.loadWorkChoices(true);},
  onSelectWork(e){const id=e.currentTarget.dataset.id;if(!this.data.workChoices.some(x=>x.id===id))return;this.onCloseWorkPicker();return this.loadJob(id);},
  onCommunity(){wx.switchTab({url:'/pages/community/community'});},
  async onSubmit(){
    if(this.data.submitting||!this.data.job)return;
    if(!this.data.consent){wx.showToast({title:'请先确认公开展示授权',icon:'none'});return;}
    if(!this.data.title.trim()){wx.showToast({title:'请填写作品标题',icon:'none'});return;}
    this.setData({submitting:true,submitError:''});
    const body={job_id:this.data.jobId,title:this.data.title.trim(),story:this.data.story.trim(),
      category:this.data.categories[this.data.categoryIndex].id,share_original:this.data.shareOriginal,consent:true};
    try{
      const r=await api.request('/api/community/submissions',{method:'POST',data:body});
      app.globalData.communityDirty=true;
      if(this._unloaded)return;
      this.setData({consent:false});await this.loadMine();
      if(this._visible!==false)wx.showToast({title:r.submission.status==='published'?'该作品已发布':'投稿已登记，审核后展示',icon:'none'});
    }catch(e){if(!this._unloaded)this.setData({submitError:(e.message||'提交暂未完成')+'；可在下方核对投稿状态，同一作品不会重复登记。'});}
    finally{if(!this._unloaded)this.setData({submitting:false});}
  },
  async loadMine(more=false){
    more=more===true;if(more&&(!this.data.hasMore||this.data.loading))return;
    const version=(this._mineVersion||0)+1;this._mineVersion=version;const offset=more?(this._nextOffset||0):0;
    this.setData({loading:true,loadError:''});
    try{
      const r=await api.request('/api/community/submissions/mine?limit=30&offset='+offset);
      if(this._unloaded||version!==this._mineVersion)return;
      const items=(r.items||[]).map(x=>({...x,statusLabel:labels[x.status]||x.status}));
      const merged=more?[...this.data.items,...items]:items;
      const current=this.data.categories[this.data.categoryIndex]||{id:'all'};
      const categories=[{id:'all',name:'按模板自动分组'},...(Array.isArray(r.categories)?r.categories:[]).filter(x=>x&&x.id!=='all')];
      if(current.id!=='all'&&!categories.some(x=>x.id===current.id))categories.push({id:current.id,name:'原分类（保留）'});
      this._nextOffset=r.next_offset;this.setData({items:[...new Map(merged.map(x=>[x.id,x])).values()],hasMore:!!r.has_more,categories,categoryIndex:Math.max(0,categories.findIndex(x=>x.id===current.id))});
    }catch(e){if(!this._unloaded&&version===this._mineVersion)this.setData({loadError:e.message||'投稿列表读取失败'});}
    finally{if(!this._unloaded&&version===this._mineVersion)this.setData({loading:false});}
  },
  onLoadMore(){return this.loadMine(true);},
  onRetrySubmission(e){const item=this.data.items.find(x=>x.id===e.currentTarget.dataset.id);if(item)this.loadJob(item.job_id,item);},
  onWithdraw(e){
    const item=this.data.items.find(x=>x.id===e.currentTarget.dataset.id);if(!item)return;
    wx.showModal({title:'撤回社区投稿',content:'撤回后社区停止展示，独立图片副本会清理；已被他人下载或分享的副本不会随之收回。已发放的精选奖励不重复发放。',confirmText:'确认撤回',
      success:async r=>{
        if(!r.confirm||this._withdrawBusy)return;this._withdrawBusy=true;
        try{await api.request('/api/community/submissions/'+encodeURIComponent(item.id)+'/withdraw',{method:'POST',data:{revision:item.revision}});app.globalData.communityDirty=true;if(!this._unloaded)await this.loadMine();}
        catch(e){if(!this._unloaded)wx.showToast({title:e.message||'撤回失败，请重试',icon:'none'});}
        finally{this._withdrawBusy=false;}
      }});
  }
});
