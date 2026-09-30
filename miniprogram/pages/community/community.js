const app = getApp();
const api = require('../../utils/api.js');

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
    loading: true
  },

  onLoad() {
    this.loadCommunity();
  },

  onShow() {
    if (this._communityLoaded) this.loadCommunity(true);
  },

  onPullDownRefresh() {
    this._mediaRetries=new Set();
    this.loadCommunity(true).then(() => wx.stopPullDownRefresh());
  },

  /** 拉取沙龙展品：后端未开启或无内容时返回空列表，不做任何本地兜底 */
  loadCommunity(force = false) {
    if (!force && this._lastLoadedAt && Date.now() - this._lastLoadedAt < 30000) return Promise.resolve();
    const version=(this._loadVersion||0)+1;this._loadVersion=version;
    this.setData({loading:true,loadError:''});
    return (async()=>{
      let offset=0,items=[],d;
      do{
        d=await api.request('/api/community'+(offset?'?offset='+offset:''),{timeout:8000});
        if(this._unloaded||version!==this._loadVersion)return;
        items.push(...((d&&d.items)||[]));
        if(d&&d.has_more){const next=Number(d.next_offset);if(!Number.isInteger(next)||next<=offset)throw new Error('社区分页异常');offset=next;}
      }while(d&&d.has_more);
      items=items.map(item=>({...item,resultUrl:api.absolute(item.resultUrl),origUrl:api.absolute(item.origUrl),authorAvatar:item.authorAvatar?api.absolute(item.authorAvatar):''}));
      this._communityLoaded=true;
      this.setData({enabled:!!(d&&d.enabled),featuredReward:d&&typeof d.featured_reward==='number'?d.featured_reward:this.data.featuredReward,
        items:[...new Map(items.map(x=>[x.id,x])).values()],loading:false});
      this.filterItems(this.data.activeFilter);this._lastLoadedAt=Date.now();
    })().catch(()=>{if(!this._unloaded&&version===this._loadVersion)this.setData({loading:false,loadError:'社区加载失败，请重试'});});
  },
  onRetry(){return this.loadCommunity(true);},
  onUnload(){this._unloaded=true;this._loadVersion=(this._loadVersion||0)+1;},
  onContribute(){wx.navigateTo({url:'/pages/works/works'});},
  onMySubmissions(){wx.navigateTo({url:'/pages/community-submit/community-submit'});},

  onSelectFilter(e) {
    const fid = e.currentTarget.dataset.id;
    if (fid === this.data.activeFilter) return;
    this.setData({ activeFilter: fid });
    this.filterItems(fid);
  },

  filterItems(fid) {
    const all = this.data.items;
    if (fid === 'all') {
      this.setData({ filteredItems: all });
    } else {
      this.setData({
        filteredItems: all.filter((x) => x.category === fid)
      });
    }
  },

  async onLikeItem(e) {
    const id=e.currentTarget.dataset.id,item=this.data.items.find(x=>x.id===id);if(!item)return;
    this._liking=this._liking||new Set();if(this._liking.has(id))return;this._liking.add(id);
    try{
      const r=await api.request('/api/community/posts/'+encodeURIComponent(id)+'/like',{method:'PUT',data:{liked:!item.liked}});
      if(this._unloaded)return;
      this.setData({items:this.data.items.map(x=>x.id===id?{...x,likes:r.likes,liked:r.liked}:x)});
      this.filterItems(this.data.activeFilter);
    }catch(err){if(!this._unloaded){wx.showToast({title:err.message||'点赞暂未保存，请重试',icon:'none'});if(err.status===404)this.loadCommunity(true);}}
    finally{this._liking.delete(id);}
  },

  onCommunityImageError(e) {
    const id=e.currentTarget.dataset.id;if(!id)return;
    this._mediaRetries=this._mediaRetries||new Set();if(this._mediaRetries.has(id))return;
    this._mediaRetries.add(id);this.loadCommunity(true);
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
