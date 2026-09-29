const app = getApp();
const api = require('../../utils/api.js');

Page({
  data: {
    templateId: '',
    template: null,
    suitableList: [],
    unsuitableList: [],
    bannerIndex: 0,
    loading: true
  },

  onLoad(options) {
    const tid = options.id || 't_anime_dots';
    this.setData({ templateId: tid });
    this.loadTemplateDetail(tid);
  },

  loadTemplateDetail(tid) {
    this.setData({ loading: true });
    api.templates()
      .then((data) => {
        const items = (data && data.items) || [];
        let tpl = items.find((t) => t.id === tid);
        if (!tpl && items.length > 0) {
          tpl = items[0];
        }

        if (tpl) {
          const rawCovers = (Array.isArray(tpl.covers) && tpl.covers.length > 0)
            ? tpl.covers
            : (tpl.cover ? [tpl.cover] : []);
          const coverUrls = rawCovers.map((c) => api.absolute(c)).filter(Boolean);
          if (coverUrls.length === 0) coverUrls.push('/images/logo.jpg');

          const fullTpl = Object.assign({}, tpl, {
            covers: coverUrls,
            coverUrl: coverUrls[0]
          });

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

          this.setData({
            template: fullTpl,
            suitableList: suitable,
            unsuitableList: unsuitable,
            loading: false
          });

          if (tpl.name) {
            wx.setNavigationBarTitle({
              title: tpl.name
            });
          }
        } else {
          this.setData({ loading: false });
        }
      })
      .catch((err) => {
        console.warn('加载模板详情失败', err);
        this.setData({ loading: false });
      });
  },

  onChoosePhoto() {
    wx.chooseMedia({
      count: 1,
      mediaType: ['image'],
      sourceType: ['album', 'camera'],
      sizeType: ['compressed', 'original'],
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
    this.setData({ bannerIndex: e.detail.current });
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
