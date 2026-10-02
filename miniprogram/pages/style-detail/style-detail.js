const app = getApp();
const api = require('../../utils/api.js');
const update=(page,patch)=>typeof api.setDataStable==='function'?api.setDataStable(page,patch):page.setData(patch);

Page({
  data: {
    templateId: '',
    template: null,
    suitableList: [],
    unsuitableList: [],
    bannerIndex: 0,
    bannerHeight: 420,
    priceLabel: '按档位计费',
    loading: true,
    loadError: ''
  },

  onLoad(options) {
    const tid = options.id || 't_anime_dots';
    update(this,{ templateId: tid });
    this.loadTemplateDetail(tid);
  },

  loadTemplateDetail(tid) {
    const version=(this._loadVersion||0)+1;this._loadVersion=version;
    update(this,{ loading: true,loadError:'',template:null });
    return api.templates()
      .then((data) => {
        if(this._unloaded||version!==this._loadVersion)return;
        const items = (data && data.items) || [];
        let tpl = items.find((t) => t.id === tid);

        if (tpl) {
          const rawCovers = (Array.isArray(tpl.covers) && tpl.covers.length > 0)
            ? tpl.covers
            : (tpl.cover ? [tpl.cover] : []);
          this._coverSources=rawCovers.map(c=>api.absolute(c));this._coverVersion=tpl.cover_version;
          const coverUrls = rawCovers.map((c) => typeof api.communityImage==='function'?api.communityImage(c,tpl.cover_version):api.absolute(c)).filter(Boolean);
          if (coverUrls.length === 0) coverUrls.push('/images/logo.jpg');

          const fullTpl = Object.assign({}, tpl, {
            covers: coverUrls,
            coverUrl: coverUrls[0]
          });
          this._coverHeights = {};

          const guide = tpl.guide || {};
          let suitable = (guide.suitable && guide.suitable.length > 0)
            ? guide.suitable
            : [
                '单人正脸、侧脸或半身人像',
                '人物五官清楚、脸部没有严重遮挡',
                '发型轮廓明显，头发细节丰富',
                '带有眼镜、发箍、耳饰等辨识度配件',
                '表情自然或具有明显情绪'
              ];

          let unsuitable = guide.unsuitable || [];

          update(this,{
            template: fullTpl,
            suitableList: suitable,
            unsuitableList: unsuitable,
            bannerIndex: 0,
            bannerHeight: 420,
            priceLabel: this.priceLabelFor(tpl),
            loading: false
          });

          // 模板价与默认档位价可能由后台热更新；用服务端当前配置刷新角标。
          if (typeof api.config === 'function') {
            api.config().then((config) => {
              if (this.data.templateId !== tid || !config) return;
              update(this,{ priceLabel: this.priceLabelFor(tpl, config) });
            }).catch(() => {});
          }

          if (tpl.name) {
            wx.setNavigationBarTitle({
              title: tpl.name
            });
          }
        } else {
          update(this,{ loading: false,loadError:'这款风格已下架或暂不可用，请返回风格库选择其他风格' });
        }
      })
      .catch((err) => {
        if(this._unloaded||version!==this._loadVersion)return;
        console.warn('加载模板详情失败', err);
        update(this,{ loading: false,loadError:'风格加载失败，请检查网络后重试' });
      });
  },

  onUnload() {this._unloaded=true;this._loadVersion=(this._loadVersion||0)+1;},
  onRetryTemplate() {return this.loadTemplateDetail(this.data.templateId);},
  onTemplateLibrary() {wx.switchTab({url:'/pages/templates/templates'});},

  onChoosePhoto() {
    if(this.data.loading||!this.data.template||this.data.loadError){wx.showToast({title:'请先确认所选风格',icon:'none'});return;}
    wx.chooseMedia({
      count: 1,
      mediaType: ['image'],
      sourceType: ['album', 'camera'],
      sizeType: ['original', 'compressed'],
      success: (res) => {
        const file = res.tempFiles[0];
        if (!file || !file.tempFilePath) return;

        const tid = (this.data.template && this.data.template.id) || this.data.templateId || '';
        wx.navigateTo({
          url: `/pages/adjust/adjust?image=${encodeURIComponent(file.tempFilePath)}&templateId=${encodeURIComponent(tid)}`
        });
      },
      fail: (err) => {
        if (err && err.errMsg && err.errMsg.indexOf('cancel') >= 0) return;
        wx.showToast({ title: '选择照片失败', icon: 'none' });
      }
    });
  },

  onBannerSwiperChange(e) {
    const index = Number(e.detail.current) || 0;
    const height = this._coverHeights && this._coverHeights[index];
    update(this,height ? { bannerIndex: index, bannerHeight: height } : { bannerIndex: index });
  },

  onCoverLoad(e) {
    const slot=Number(e.currentTarget.dataset.index)||0;
    if(typeof api.rememberCommunityImage==='function'&&this._coverSources)api.rememberCommunityImage(this._coverSources[slot],this._coverVersion).catch(()=>{});
    const info = e.detail || {};
    const width = Number(info.width);
    const height = Number(info.height);
    if (!(width > 0 && height > 0)) return;
    const index = Number(e.currentTarget.dataset.index) || 0;
    // 卡片横向内距各 28rpx：实际可用宽度为 750-56=694rpx。
    const scaled = Math.max(320, Math.min(1300, Math.round(694 * height / width)));
    if (!this._coverHeights) this._coverHeights = {};
    this._coverHeights[index] = scaled;
    if (index === this.data.bannerIndex && this.data.bannerHeight !== scaled) {
      update(this,{ bannerHeight: scaled });
    }
  },

  priceLabelFor(tpl, config) {
    const conf = config || {};
    const freeMode = typeof conf.free_mode === 'boolean' ? conf.free_mode : !!app.globalData.freeMode;
    if (freeMode) return '免扣费';
    const prices = conf.prices || {};
    const light=prices.light!=null?prices.light:40;
    const fine=prices.fine!=null?prices.fine:40;
    const low=Math.min(light,fine),high=Math.max(light,fine);
    return `✦ ${low===high?low:low+'–'+high} 光子`;
  },

  onPreviewCover(e) {
    const current = e.currentTarget.dataset.url;
    const covers = (this.data.template && this.data.template.covers) || [];
    if (!covers.length) return;
    wx.previewImage({
      current: current || covers[0],
      urls: covers
    });
  }
});
