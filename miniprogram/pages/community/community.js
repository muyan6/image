const app = getApp();
const api = require('../../utils/api.js');
const update=(page,patch)=>typeof api.setDataStable==='function'?api.setDataStable(page,patch):page.setData(patch);

/**
 * 灵感沙龙（社区）
 *
 * 帖子来自 GET /api/community，在 /admin「社区管理」中逐帖维护，
 * 前端不再内置任何示例/测试文案（此前是 4 条写死的假展品）。
 * 暂停展示和已删除的帖子不会返回；置顶与排序由服务端决定。
 */
Page({
  data: {
    filters: [
      { id: 'all', name: '全部展品' },
      { id: 'liked', name: '我喜欢的' },
      { id: 'portrait', name: '冷白人像' },
      { id: 'film', name: '复古胶片' },
      { id: 'old_photo', name: '老照片复苏' },
      { id: 'anime', name: '动漫重绘' }
    ],
    activeFilter: 'all',
    items: [],
    filteredItems: [],
    enabled: false,
    featuredReward: 50,
    loadError: '',
    loading: true,loadingMore:false,loaded:false,hasMore:false,total:0
  },

  onLoad() {
    this._visible=true;
    this.loadCommunity();
  },

  onShow() {
    this._visible=true;
    if (app.globalData.communityUpdated) {
      const delta=app.globalData.communityUpdated;
      update(this,{items:this.data.items.map(x=>x.id===delta.id?{...x,...delta}:x)});
      this.filterItems(this.data.activeFilter);app.globalData.communityUpdated=null;
    }
    if (this._communityLoaded||this._needsRefreshOnShow) this.loadCommunity(!!app.globalData.communityDirty||!!this._needsRefreshOnShow);
    this._needsRefreshOnShow=false;
    app.globalData.communityDirty=false;
  },
  onHide(){
    this._visible=false;this._needsRefreshOnShow=true;this._loadVersion=(this._loadVersion||0)+1;
    this._loadingCommunity=null;this._loadingFilter=null;update(this,{loading:false,loadingMore:false});
  },

  onPullDownRefresh() {
    this._mediaRetries=new Set();
    this.loadCommunity(true).then(() => wx.stopPullDownRefresh());
  },

  /** 拉取沙龙展品：后端未开启或无内容时返回空列表，不做任何本地兜底 */
  loadCommunity(force = false, more = false) {
    more=more===true;
    if(more&&(!this.data.hasMore||this._loadingCommunity))return Promise.resolve();
    if (!force && this._lastLoadedAt && Date.now() - this._lastLoadedAt < 30000) return Promise.resolve();
    if (this._loadingCommunity && this._loadingFilter===this.data.activeFilter) return this._loadingCommunity;
    const version=(this._loadVersion||0)+1;this._loadVersion=version;
    update(this,{loading:!more&&!this.data.items.length,loadingMore:more,loadError:''});
    const task=(async()=>{
      const offset=more?(this._nextOffset||0):0,fid=this.data.activeFilter;
      const query='?limit=24&offset='+offset+(fid==='liked'?'&liked_only=true':fid!=='all'?'&category='+encodeURIComponent(fid):'');
      const d=await api.request('/api/community'+query,{timeout:8000});
      if(this._unloaded||this._visible===false||version!==this._loadVersion)return;
      if(!d||!Array.isArray(d.items))throw new Error('社区列表数据异常');
      if(d.has_more&&(!Number.isInteger(Number(d.next_offset))||Number(d.next_offset)<=offset))throw new Error('社区分页异常');
      let items=d.items;
      items=items.map(item=>{
        const prior=this.data.items.find(x=>x.id===item.id);
        const display=(url,old)=>{
          const next=api.absolute(url);
          if(typeof api.stableImageUrl==='function')return api.stableImageUrl(next,old);
          if(typeof api.communityImage==='function')return api.communityImage(next);
          return old&&old===next?old:next;
        };
        // Remote signatures stay private to image-load handlers; they are not visual data.
        const small=this._thumbnailFallbacks&&this._thumbnailFallbacks.has(item.id)?item.resultUrl:(item.thumbnailUrl||item.resultUrl);
        this._imageSources=this._imageSources||{};this._imageSources[item.id]={result:api.absolute(small),orig:api.absolute(item.origUrl)};
        return {...item,
          thumbnailUrl:display(small,prior&&prior.thumbnailUrl),
          resultUrl:display(item.resultUrl,prior&&prior.resultUrl),origUrl:display(item.origUrl,prior&&prior.origUrl),
          authorAvatar:item.authorAvatar?api.absolute(item.authorAvatar):''};
      });
      this._communityLoaded=true;
      this._nextOffset=Number(d.next_offset)||0;
      update(this,{enabled:!!(d&&d.enabled),featuredReward:d&&typeof d.featured_reward==='number'?d.featured_reward:this.data.featuredReward,
        items:[...new Map((more?this.data.items.concat(items):items).map(x=>[x.id,x])).values()],loading:false,
        loaded:true,total:Number(d.total)||items.length,hasMore:!!d.has_more});
      this.filterItems(this.data.activeFilter);this._lastLoadedAt=Date.now();
    })().catch(()=>{if(!this._unloaded&&this._visible!==false&&version===this._loadVersion)update(this,{loading:false,loadError:'社区加载失败，请重试'});}).finally(()=>{
      if(this._loadingCommunity===task)this._loadingCommunity=null;
      if(!this._unloaded&&this._visible!==false&&version===this._loadVersion)update(this,{loading:false,loadingMore:false});
    });
    this._loadingFilter=this.data.activeFilter;this._loadingCommunity=task;return task;
  },
  onReachBottom(){return this.loadCommunity(true,true);},
  onMorePosts(){return this.loadCommunity(true,true);},
  onRetry(){return this.loadCommunity(true);},
  onUnload(){this._unloaded=true;this._loadVersion=(this._loadVersion||0)+1;},
  onContribute(){wx.navigateTo({url:'/pages/works/works'});},
  onMySubmissions(){wx.navigateTo({url:'/pages/community-submit/community-submit'});},

  onSelectFilter(e) {
    const fid = e.currentTarget.dataset.id;
    if (fid === this.data.activeFilter) return;
    if(!this.data.filters.some(f=>f.id===fid))return;
    this._lastLoadedAt=0;
    update(this,{ activeFilter: fid,items:[],filteredItems:[],loaded:false,hasMore:false });
    return this.loadCommunity(true);
  },

  filterItems(fid) {
    const all = this.data.items;
    if (fid === 'all') {
      update(this,{ filteredItems: all });
    } else {
      update(this,{
        filteredItems: all.filter((x) => fid==='liked'?x.liked:x.category === fid)
      });
    }
  },

  async onLikeItem(e) {
    const id=e.currentTarget.dataset.id,item=this.data.items.find(x=>x.id===id);if(!item)return;
    this._liking=this._liking||new Set();if(this._liking.has(id))return;this._liking.add(id);
    try{
      const r=await api.request('/api/community/posts/'+encodeURIComponent(id)+'/like',{method:'PUT',data:{liked:!item.liked}});
      if(this._unloaded||this._visible===false)return;
      update(this,{items:this.data.items.map(x=>x.id===id?{...x,likes:r.likes,liked:r.liked}:x)});
      this.filterItems(this.data.activeFilter);
    }catch(err){if(!this._unloaded&&this._visible!==false){wx.showToast({title:err.message||'点赞暂未保存，请重试',icon:'none'});if(err.status===404)this.loadCommunity(true);}}
    finally{this._liking.delete(id);}
  },

  onCommunityImageError(e) {
    if(this._visible===false)return;
    const id=e.currentTarget.dataset.id;if(!id)return;
    const item=this.data.items.find(x=>x.id===id);
    if(e.currentTarget.dataset.kind==='result'&&item&&item.thumbnailUrl!==item.resultUrl){
      this._thumbnailFallbacks=this._thumbnailFallbacks||new Set();this._thumbnailFallbacks.add(id);
    }
    if(item&&typeof api.forgetCommunityImage==='function')api.forgetCommunityImage(this._imageSources&&this._imageSources[id]&&this._imageSources[id][e.currentTarget.dataset.kind]);
    this._mediaRetries=this._mediaRetries||new Set();if(this._mediaRetries.has(id))return;
    this._mediaRetries.add(id);this.loadCommunity(true);
  },

  onCommunityImageLoad(e) {
    const {id,kind}=e.currentTarget.dataset,item=this.data.items.find(x=>x.id===id);
    if(!item||typeof api.rememberCommunityImage!=='function')return;
    api.rememberCommunityImage(this._imageSources&&this._imageSources[id]&&this._imageSources[id][kind]).catch(()=>{});
  },
  onOpenPost(e) {
    const id=e.currentTarget.dataset.id,item=this.data.items.find(x=>x.id===id);if(!item)return;
    app.globalData.communityPreview=item;
    wx.navigateTo({url:'/pages/community-detail/community-detail?id='+encodeURIComponent(id)});
  },

  onPreviewExhibit(e) {
    const item = e.currentTarget.dataset.item;
    const type = e.currentTarget.dataset.type;
    const urls = [item.resultUrl, item.origUrl].filter(Boolean);
    if (!urls.length) return;
    const current = type === 'original' && item.origUrl
      ? item.origUrl
      : (item.resultUrl || urls[0]);
    wx.previewImage({
      current: current,
      urls: urls
    });
  },

  onShareExhibit(e) {
    const url = e.currentTarget.dataset.resultUrl;
    if (!url) {
      wx.showToast({ title: '这件作品暂无可分享的图片', icon: 'none' });
      return;
    }
    if (!wx.showShareImageMenu) {
      wx.showToast({ title: '请点击右上角【···】分享', icon: 'none' });
      return;
    }
    wx.getImageInfo({
      src: url,
      success: (info) => {
        if (!info.path) {
          wx.showToast({ title: '作品图片未加载，请重试', icon: 'none' });
          return;
        }
        wx.showShareImageMenu({ path: info.path,
          fail: () => wx.showToast({ title: '分享未完成，请重试', icon: 'none' }) });
      },
      fail: () => wx.showToast({ title: '作品图片未加载，请重试', icon: 'none' })
    });
  },

  onMakeSame(e) {
    const tid = e.currentTarget.dataset.templateId;
    const tname = e.currentTarget.dataset.name;
    if (!tid) {
      wx.showToast({ title: '该展品未绑定配方', icon: 'none' });
      return;
    }

    // 只带 id/名字；档位与价格由调整页从服务端模板数据现读，不在这里猜
    app.globalData.selectedTemplate = {
      id: tid,
      name: tname
    };

    wx.showToast({
      title: `已选用配方：${tname}`,
      icon: 'none'
    });

    setTimeout(() => {
      wx.switchTab({
        url: '/pages/index/index'
      });
    }, 400);
  }
});
