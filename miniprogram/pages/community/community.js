const app = getApp();
const api = require('../../utils/api.js');

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
    filteredItems: []
  },

  onLoad() {
    this.initCommunityData();
  },

  onPullDownRefresh() {
    this.initCommunityData();
    wx.stopPullDownRefresh();
  },

  initCommunityData() {
    // 艺术沙龙精选展品
    const baseCovers = api.apiBase() + '/api/covers/';
    const exhibits = [
      {
        id: 'c1',
        title: '暗光昏黄废片，重现冷白通透质感',
        story: '晚上咖啡馆随手抓拍的原图噪点极重、肤色偏黄暗沉。用冷白通透模式拯救后，高光变得清爽干净，毛孔和发丝边缘宛如自然光重塑。',
        authorName: '苏木',
        authorAvatar: '/images/logo.jpg',
        date: '2小时前',
        category: 'portrait',
        categoryName: '冷白人像',
        templateId: 't_clarity',
        templateName: '冷白通透',
        quality: 'fine',
        resultUrl: baseCovers + 't_clarity_v1.jpg?v=1',
        origUrl: '/images/logo.jpg',
        likes: 128,
        liked: false
      },
      {
        id: 'c2',
        title: '褪色老街抓拍 · 唤醒富士日系绿调',
        story: '阴雨天在老街拍的照片灰蒙蒙一片，选用富士胶片配方处理后，暗部被赋予了细腻温润的墨绿色调，情绪感瞬间拉满。',
        authorName: '白驹',
        authorAvatar: '/images/logo.jpg',
        date: '昨天',
        category: 'film',
        categoryName: '复古胶片',
        templateId: 't_fuji',
        templateName: '富士经典',
        quality: 'fine',
        resultUrl: baseCovers + 't_fuji_v1.jpg?v=1',
        origUrl: '/images/logo.jpg',
        likes: 246,
        liked: false
      },
      {
        id: 'c3',
        title: '午后随拍小猫 · 一键走进吉卜力世界',
        story: '原本过曝失焦的家猫随手拍，通过动漫重绘风格转化成水彩手绘风，像直接从宫崎骏电影手稿里走出来一样梦幻。',
        authorName: '青禾',
        authorAvatar: '/images/logo.jpg',
        date: '3天前',
        category: 'anime',
        categoryName: '动漫重绘',
        templateId: 't_ghibli',
        templateName: '吉卜力童话',
        quality: 'light',
        resultUrl: baseCovers + 't_ghibli_v1.jpg?v=1',
        origUrl: '/images/logo.jpg',
        likes: 319,
        liked: false
      },
      {
        id: 'c4',
        title: '上世纪八十年代老底片 · 2K发丝级复活',
        story: '泛黄卷边的旧家庭合照，经由超分重构算法去除折痕与划伤，人物眼眸和发丝根根分明，记忆再次清晰浮现。',
        authorName: '林深',
        authorAvatar: '/images/logo.jpg',
        date: '5天前',
        category: 'old_photo',
        categoryName: '老照片复苏',
        templateId: 't_master',
        templateName: '深度超分',
        quality: 'fine',
        resultUrl: baseCovers + 't_master_v1.jpg?v=1',
        origUrl: '/images/logo.jpg',
        likes: 482,
        liked: false
      }
    ];

    this.setData({
      items: exhibits,
      filteredItems: exhibits
    });
    this.filterItems(this.data.activeFilter);
  },

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

  onLikeItem(e) {
    const id = e.currentTarget.dataset.id;
    const list = this.data.filteredItems.map((item) => {
      if (item.id === id) {
        const nextLiked = !item.liked;
        return Object.assign({}, item, {
          liked: nextLiked,
          likes: nextLiked ? item.likes + 1 : item.likes - 1
        });
      }
      return item;
    });
    this.setData({ filteredItems: list });
    wx.showToast({
      title: '感谢赞叹',
      icon: 'none'
    });
  },

  onPreviewExhibit(e) {
    const item = e.currentTarget.dataset.item;
    const type = e.currentTarget.dataset.type;
    const current = type === 'original' ? item.origUrl : item.resultUrl;
    wx.previewImage({
      current: current,
      urls: [item.resultUrl, item.origUrl].filter(Boolean)
    });
  },

  onShareExhibit(e) {
    const title = e.currentTarget.dataset.title;
    wx.showShareImageMenu ? wx.showShareImageMenu({ path: '/images/logo.jpg' }) : wx.showToast({
      title: '请点击右上角【···】分享',
      icon: 'none'
    });
  },

  onMakeSame(e) {
    const tid = e.currentTarget.dataset.templateId;
    const tname = e.currentTarget.dataset.name;

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
