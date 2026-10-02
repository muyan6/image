const app = getApp();
const api = require('../../utils/api.js');
const preferences = require('../../utils/preferences.js');
const update=(page,patch)=>typeof api.setDataStable==='function'?api.setDataStable(page,patch):page.setData(patch);

Page({
  data: {
    loading: false,
    loadError: '',
    loaded: false,
    skeletonItems: [0, 1, 2, 3],
    categories: [
      { id: 'all', name: '全部风格', count: 0 }
    ],
    activeCategory: 'all',
    searchQuery: '',
    preferenceMode:'all', favoriteIds:[], recentIds:[], preferencesLoading:false, preferencesError:'',
    freeMode: false,
    priceLight: 40,
    priceFine: 40,
    allTemplates: [],
    filteredTemplates: [],
    selectedItem: null
  },

  onLoad() {
    // Return refreshes metadata without replacing unchanged image sources.
  },

  onShow() {
    this.fetchTemplates();
    this.loadPreferences();
  },

  onPullDownRefresh() {
    this.loadPreferences();
    this.fetchTemplates(() => {
      wx.stopPullDownRefresh();
    }, true);
  },

  async loadPreferences() {
    if(typeof preferences.load!=='function')return;
    const version=(this._preferencesVersion||0)+1;this._preferencesVersion=version;
    update(this,{preferencesLoading:true,preferencesError:''});
    try{
      const p=await preferences.load();if(version!==this._preferencesVersion)return;
      update(this,{favoriteIds:p.template_favorites,recentIds:p.recent_templates});
      this.applyPreferences();
    }catch(e){if(version===this._preferencesVersion)update(this,{preferencesError:e.message||'收藏与最近使用读取失败，请重试'});}
    finally{if(version===this._preferencesVersion)update(this,{preferencesLoading:false});}
  },
  applyPreferences(){
    const favorites=new Set(this.data.favoriteIds);
    update(this,{allTemplates:this.data.allTemplates.map(x=>({...x,favorite:favorites.has(x.id)}))});
    this.filterByCategory(this.data.activeCategory);
  },
  onPreferenceFilter(e){
    const mode=e.currentTarget.dataset.id;if(!['all','favorites','recent'].includes(mode))return;
    update(this,{preferenceMode:mode});this.filterByCategory(this.data.activeCategory);
  },
  async onFavorite(e){
    const id=e.currentTarget.dataset.id,item=this.data.allTemplates.find(x=>x.id===id);if(!item)return;
    this._favoriting=this._favoriting||new Set();if(this._favoriting.has(id))return;this._favoriting.add(id);
    try{
      const p=await preferences.setFavorite(id,!item.favorite);
      update(this,{favoriteIds:p.template_favorites,recentIds:p.recent_templates,preferencesError:''});this.applyPreferences();
      wx.showToast({title:item.favorite?'已取消收藏':'已收藏模板',icon:'none'});
    }catch(e){wx.showToast({title:e.message||'收藏暂未保存，请重试',icon:'none'});}
    finally{this._favoriting.delete(id);}
  },

  fetchTemplates(callback, force = false) {
    update(this,{ loadError: '' });
    if (!force && this._lastFetchedAt && Date.now() - this._lastFetchedAt < 30000 &&
        this.data.allTemplates.length) {
      if (typeof callback === 'function') callback();
      return;
    }
    const version = (this._fetchVersion || 0) + 1;
    this._fetchVersion = version;
    if (!this.data.allTemplates.length) update(this,{ loading: true });
    if (this.data.freeMode !== !!app.globalData.freeMode && !this._latestRawItems) {
      update(this,{ freeMode: !!app.globalData.freeMode });
    }

    // 1. 尝试读本地缓存，秒开
    try {
      const cached = wx.getStorageSync('cached_templates_data');
      if (!this.data.allTemplates.length && cached && Array.isArray(cached.items) && cached.items.length > 0) {
        this._renderData(cached.groups || [], cached.items);
        update(this,{ loaded: true });
      }
    } catch (e) {}

    // 2. 异步请求后端
    // 模板目录与价格分开获取；任意一方先回来都用最新两份数据重新渲染。
    api.config().then((config) => {
      if (version !== this._fetchVersion || !config) return;
      const prices = config.prices || {};
      app.globalData.freeMode = !!config.free_mode;
      update(this,{ freeMode: !!config.free_mode,
        priceLight: prices.light != null ? prices.light : this.data.priceLight,
        priceFine: prices.fine != null ? prices.fine : this.data.priceFine });
      if (this._latestRawItems) this._renderData(this._latestGroups, this._latestRawItems);
    }).catch(() => {});

    api.templates()
      .then((data) => {
        if (version !== this._fetchVersion) {
          if (typeof callback === 'function') callback();
          return;
        }
        const groups = (data && data.groups) || [];
        const rawItems = (data && data.items) || [];
        try { wx.setStorageSync('cached_templates_data', data); } catch (e) {}
        this._renderData(groups, rawItems);
        update(this,{ loading: false, loaded: true, loadError: '' });
        this._lastFetchedAt = Date.now();
        if (typeof callback === 'function') callback();
      })
      .catch((err) => {
        if (version !== this._fetchVersion) {
          if (typeof callback === 'function') callback();
          return;
        }
        console.warn('获取模板列表失败', err);
        update(this,{ loading: false, loadError: '模板加载失败，请检查网络后重试' });
        if (typeof callback === 'function') callback();
      });
  },

  _renderData(groups, rawItems) {
    this._latestGroups = groups;
    this._latestRawItems = rawItems;
    const prior=new Map(this.data.allTemplates.map(x=>[x.id,x]));this._coverSources={};
    const items = rawItems.map((item) => {
      const low=Math.min(this.data.priceLight,this.data.priceFine);
      const high=Math.max(this.data.priceLight,this.data.priceFine);
      const cost = this.data.freeMode ? '免扣费' : ('✦ ' + (low===high?low:low+'–'+high) + ' 光子');
      const rawCovers = Array.isArray(item.covers) && item.covers.length > 0
        ? item.covers
        : (item.cover ? [item.cover] : []);
      const old=this._coverRetries&&this._coverRetries.has(item.id)?null:prior.get(item.id),version=item.cover_version;
      const small=this._coverRetries&&this._coverRetries.has(item.id)?rawCovers[0]:(item.thumbnail||item.thumbnailUrl||rawCovers[0]);
      this._coverSources[item.id]={urls:rawCovers.map(c=>api.absolute(c)),thumbnail:api.absolute(small),version};
      const coverUrls = rawCovers.map((c,i) => typeof api.stableImageUrl==='function'
        ? api.stableImageUrl(c,old&&old.covers&&old.covers[i],version,old&&old.cover_version) : api.absolute(c)).filter(Boolean);
      return Object.assign({}, item, {
        cover: coverUrls[0] || '',
        covers: coverUrls,
        thumbnail: typeof api.stableImageUrl==='function'?api.stableImageUrl(small,old&&old.thumbnail,version,old&&old.cover_version):api.absolute(small),
        coverUrl: (typeof api.stableImageUrl==='function'?api.stableImageUrl(small,old&&old.coverUrl,version,old&&old.cover_version):api.absolute(small)) || coverUrls[0] || (item.cover ? api.absolute(item.cover) : '/images/logo.jpg'),
        costText: cost, favorite:this.data.favoriteIds.includes(item.id)
      });
    });

    const cats = [
      { id: 'all', name: '全部风格', count: items.length }
    ];

    groups.forEach((g) => {
      const count = items.filter((x) => x.group_id === g.id).length;
      cats.push({
        id: g.id,
        name: g.name,
        count: count
      });
    });

    const category = cats.some((c) => c.id === this.data.activeCategory) ? this.data.activeCategory : 'all';
    // Sort before comparing/committing, so identical data never remounts the grid.
    items.sort((a,b) => (Number(b.usage_count)||0) - (Number(a.usage_count)||0));
    update(this,{
      allTemplates: items,
      categories: cats,
      activeCategory: category
    });
    this.filterByCategory(category);
  },

  onTemplateImageLoad(e) {
    const source=this._coverSources&&this._coverSources[e.currentTarget.dataset.id];
    if(source&&typeof api.rememberCommunityImage==='function')api.rememberCommunityImage(source.thumbnail,source.version).catch(()=>{});
  },
  onTemplateImageError(e) {
    const id=e.currentTarget.dataset.id,source=this._coverSources&&this._coverSources[id];
    this._coverRetries=this._coverRetries||new Set();if(this._coverRetries.has(id)||!source)return;this._coverRetries.add(id);
    if(typeof api.forgetCommunityImage==='function')api.forgetCommunityImage(source.thumbnail,source.version);
    return this.fetchTemplates(null,true);
  },

  onSelectCategory(e) {
    const cid = e.currentTarget.dataset.id;
    if (cid === this.data.activeCategory) return;
    update(this,{ activeCategory: cid });
    this.filterByCategory(cid);
  },

  filterByCategory(cid) {
    const words = this.data.searchQuery.trim().toLowerCase().split(/\s+/).filter(Boolean);
    const list = this.data.allTemplates.filter(item => {
      if (cid !== 'all' && item.group_id !== cid) return false;
      if(this.data.preferenceMode==='favorites'&&!this.data.favoriteIds.includes(item.id))return false;
      if(this.data.preferenceMode==='recent'&&!this.data.recentIds.includes(item.id))return false;
      const text = [item.name, item.subtitle, item.group_name, item.id,
        Array.isArray(item.tags) ? item.tags.join(' ') : item.tags || ''].join(' ').toLowerCase();
      return words.every(word => text.includes(word));
    });
    if(this.data.preferenceMode==='recent')list.sort((a,b)=>this.data.recentIds.indexOf(a.id)-this.data.recentIds.indexOf(b.id));
    update(this,{ filteredTemplates: list });
  },

  onRetryTemplates() {
    this.fetchTemplates(null, true);
  },

  onSearchInput(e) {
    update(this,{searchQuery: (e.detail.value || '').slice(0, 40)});
    this.filterByCategory(this.data.activeCategory);
  },

  onClearSearch() {
    update(this,{searchQuery: ''});
    this.filterByCategory(this.data.activeCategory);
  },

  onOpenDetail(e) {
    const item = e.currentTarget.dataset.template;
    if (!item) return;
    wx.navigateTo({
      url: `/pages/style-detail/style-detail?id=${encodeURIComponent(item.id)}`
    });
  },

  onCloseDetail() {
    update(this,{ selectedItem: null });
  },

  onApplyTemplate() {
    const tpl = this.data.selectedItem;
    if (!tpl) return;
    update(this,{ selectedItem: null });
    wx.navigateTo({
      url: `/pages/style-detail/style-detail?id=${encodeURIComponent(tpl.id)}`
    });
  },

  onPreviewCovers(e) {
    const current = e.currentTarget.dataset.url;
    const covers = (this.data.selectedItem && this.data.selectedItem.covers) || [];
    if (!covers.length) return;
    wx.previewImage({
      current: current || covers[0],
      urls: covers
    });
  }
});
