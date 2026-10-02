const app = getApp();
const api = require('../../utils/api.js');
const creation = require('../../utils/creation-draft.js');
const update=(page,patch)=>typeof api.setDataStable==='function'?api.setDataStable(page,patch):page.setData(patch);

const DEMO_ORIG = 'https://images.unsplash.com/photo-1534438327276-14e5300c3a48?w=800';
const DEMO_RESULT = 'https://images.unsplash.com/photo-1534438327276-14e5300c3a48?w=800&q=80';

Page({
  data: {
    originalUrl: '',
    resultUrl: '',
    quality: 'fine',
    splitPercent: 50,
    demo: false,
    saving: false,
    recreating: false,
    label: '精细修复',
    originalUnavailable: false,
    originalCompressed: false,
    textGenerated:false,
    resultError: '',
    originalError: '',
    loadingMedia: false,
    isDragging: false,
    isPressingOriginal: false,
    // 舞台实测宽度(px)：clip-mat 内层原画按整块舞台锁定，避免裁剪后重新缩放
    stageW: 0
  },

  onLoad(options) {
    const rawOrig = options.original ? decodeURIComponent(options.original) : '';
    const rawRes = options.result ? decodeURIComponent(options.result) : '';
    const orig = api.cleanUrl ? api.cleanUrl(rawOrig) : rawOrig;
    const res = api.cleanUrl ? api.cleanUrl(rawRes) : rawRes;
    const quality = options.quality === 'light' ? 'light' : 'fine';
    const demo = !!options.demo || (!options.job && !orig && !res);

    this._jobId = options.job ? decodeURIComponent(options.job) : '';
    this._mediaCache = (app.globalData.mediaCache && app.globalData.mediaCache[this._jobId]) || {};
    this._reloadAttempts = {};
    this._unloaded = false;

    // 首帧兜底：onReady 的实测尺寸回来之前，先用窗口尺寸撑住，
    // 否则内层原图 width:0 会整张不显示，出现半屏空白
    let winW = 375;
    try {
      const info = wx.getWindowInfo ? wx.getWindowInfo() : wx.getSystemInfoSync();
      winW = info.windowWidth || winW;
    } catch (e) { /* 取不到就用默认值，onReady 会立刻纠正 */ }

    update(this,{
      originalUrl: demo ? DEMO_ORIG : (this._jobId ? (this._mediaCache.orig || '') : orig),
      resultUrl: demo ? DEMO_RESULT : (this._jobId ? (this._mediaCache.result || '') : res),
      quality: quality,
      demo: demo,
      label: quality === 'fine' ? '精细修复' : '轻量修复',
      stageW: winW
    });

    // 历史里存的是短期签名，不能直接当作永久图片；先刷新再下载为本地临时文件。
    if (!demo) this.refreshUrls();
  },

  onShow() {
    if (!this.data.demo) this.refreshUrls();
  },

  onUnload() {
    this._unloaded = true;
    this._mediaCache = {};
  },

  /** 本地文件在当前页面复用；再次进入页面会重新获取签名，不存过期链接。 */
  refreshUrls(force = false) {
    if (!this._jobId || this.data.demo) return Promise.resolve(false);
    if (this._refreshPromise) return this._refreshPromise;
    if (!force && this._lastMediaRefreshAt && Date.now() - this._lastMediaRefreshAt < 60000 &&
        this.data.resultUrl && !this.data.resultError &&
        (typeof api.isLocalImageAvailable!=='function'||api.isLocalImageAvailable(this.data.resultUrl))) return Promise.resolve(true);
    this._mediaCache = this._mediaCache || {};
    update(this,{loadingMedia: !this.data.resultUrl});
    this._refreshPromise = api.request('/api/jobs/' + encodeURIComponent(this._jobId))
      .then(async (job) => {
        if (this._unloaded) return false;
        if (!job || job.status !== 'succeeded') throw new Error('作品尚未完成');
        if (!job.result_url) throw new Error('作品已到保存期限');
        const load = (kind, url) => {
          if (this._mediaCache[kind] && (typeof api.isLocalImageAvailable!=='function'||api.isLocalImageAvailable(this._mediaCache[kind]))) return Promise.resolve(this._mediaCache[kind]);
          delete this._mediaCache[kind];
          return api.downloadJobMedia(this._jobId, kind, url).then((path) => {
            if (!this._unloaded) this._mediaCache[kind] = path;
            return path;
          });
        };
        const dimensions = job.width && job.height ? `${job.width} × ${job.height}` : '';
        update(this,{originalUnavailable: !job.orig_url,originalCompressed:!!job.comparison_compressed,textGenerated:job.input_mode==='text',
          label: (job.input_mode === 'text' ? 'AI 文生图' : (job.provider === 'local' ? '本地增强' : 'AI 修复')) + (dimensions ? ' · ' + dimensions : '')});
        // 两侧独立更新：原图下载缓慢/故障也不能阻断成品展示。
        const outcomes = await Promise.all([
          load('result', job.result_url).then(path => {
            if (!this._unloaded) update(this,Object.assign({resultUrl:path, resultError:''},
              job.orig_url ? {} : {originalUrl:path}));
            return {path};
          }, error => {
            if (!this._unloaded) update(this,{resultError:error.message || '结果图加载失败'});
            return {error};
          }),
          job.orig_url ? load('orig', job.orig_url).then(path => {
            if (!this._unloaded) update(this,{originalUrl:path, originalError:''});
            return {path};
          }, error => {
            if (!this._unloaded) update(this,{originalError:error.message || '原图加载失败'});
            return {error};
          }) : Promise.resolve({expired:true})
        ]);
        if (this._unloaded) return false;
        const result = outcomes[0], original = outcomes[1];
        update(this,{ originalUrl: original.expired ? (result.path || '') : (original.path || ''),
          resultUrl: result.path || '',
          resultError: result.error ? result.error.message || '结果图加载失败' : '',
          originalError: original.error ? original.error.message || '原图加载失败' : '',
          originalUnavailable: !job.orig_url,
          originalCompressed:!!job.comparison_compressed,
          textGenerated:job.input_mode==='text',
          label: (job.input_mode === 'text' ? 'AI 文生图' : (job.provider === 'local' ? '本地增强' : 'AI 修复')) + (dimensions ? ' · ' + dimensions : '') });
        if (result.path) this._lastMediaRefreshAt = Date.now();
        return !!result.path;
      })
      .catch((err) => {
        if (!this._unloaded) update(this,{resultError: err.message || '图片加载失败，请重试'});
        return false;
      }).finally(() => {
        this._refreshPromise = null;
        if (!this._unloaded) update(this,{loadingMedia: false});
      });
    return this._refreshPromise;
  },

  onReady() {
    this.measure();
  },

  /**
   * 量取对比舞台的精确尺寸。
   *
   * stageW 会写进 data，作为 clip-mat 内层原画的固定像素宽度（高度交给 CSS 100%，
   * 与底层结果图同源，不会出现兜底值与真实值不一致的中间帧）：
   * 原图与结果图必须共享同一个 aspectFit 缩放基准（整块舞台），
   * 否则被裁到一半的原图会按「可见宽度」重新缩放，左右两半高度对不上。
   */
  measure() {
    return new Promise((resolve) => {
      wx.createSelectorQuery()
        .select('#stage')
        .boundingClientRect((rect) => {
          if (rect && rect.width > 0) {
            this._rect = rect;
            if (rect.width !== this.data.stageW) {
              update(this,{ stageW: rect.width });
            }
          }
          resolve(this._rect || null);
        })
        .exec();
    });
  },

  onTouchStart(e) {
    update(this,{ isDragging: true });
    try {
      wx.vibrateShort({ type: 'light' });
    } catch (err) {}
    this.measure().then(() => this.moveTo(e));
  },

  onTouchMove(e) {
    this.moveTo(e);
  },

  onTouchEnd() {
    update(this,{ isDragging: false });
  },

  /** 左右拖动滑块 */
  moveTo(e) {
    const touch = e.touches && e.touches[0];
    const rect = this._rect;
    if (!rect || !touch) return;

    const raw = ((touch.clientX - rect.left) / rect.width) * 100;
    const percent = Math.max(0, Math.min(100, Math.round(raw)));
    if (percent === this.data.splitPercent) return;
    update(this,{ splitPercent: percent });
  },

  /** 长按即时查看原图 (Lightroom 交互) */
  onPressOriginalStart() {
    try {
      wx.vibrateShort({ type: 'light' });
    } catch (err) {}
    update(this,{ isPressingOriginal: true });
  },

  onPressOriginalEnd() {
    update(this,{ isPressingOriginal: false });
  },

  onChangePhoto() {
    wx.navigateBack();
  },

  async onRecreate() {
    if(this.data.recreating||this.data.demo||!this._jobId)return;
    update(this,{recreating:true});
    try {await creation.recreateFromJob(this._jobId);}
    catch(e){if(!this._unloaded)wx.showModal({title:'创作参数未恢复',content:e.message||'请稍后重试',showCancel:false});}
    finally {if(!this._unloaded)update(this,{recreating:false});}
  },

  onClose() {
    wx.navigateBack();
  },

  onResultError() {
    this.reloadMedia('result');
  },

  onOrigError() {
    this.reloadMedia(this.data.originalUnavailable ? 'result' : 'orig');
  },

  reloadMedia(kind) {
    if (this.data.demo || this._unloaded || this._refreshPromise) return;
    this._reloadAttempts = this._reloadAttempts || {};
    this._mediaCache = this._mediaCache || {};
    delete this._mediaCache[kind];
    const field = kind === 'result' ? 'resultError' : 'originalError';
    if (this._reloadAttempts[kind]) {
      update(this,{[field]: '图片加载失败，点击重试'}); return;
    }
    this._reloadAttempts[kind] = 1;
    this.refreshUrls(true);
  },

  onRetryMedia() {
    this._reloadAttempts = {};
    this._mediaCache = {};
    this.refreshUrls(true);
  },

  /** 保存高清修复照片到系统相册 */
  async onDownload() {
    if (this.data.saving) return;
    if (this.data.demo) {
      wx.showToast({ title: '演示模式下无本地原画可保存', icon: 'none' });
      return;
    }

    try {
      wx.vibrateShort({ type: 'medium' });
    } catch (err) {}

    update(this,{ saving: true });
    wx.showLoading({ title: '正在导出原画...', mask: true });

    const done = () => {
      update(this,{ saving: false });
      wx.hideLoading();
    };

    try {
      // 已展示的本地成品直接保存，不重复下载，也不等待原图加载。
      let target = this._mediaCache && this._mediaCache.result;
      if (!target) {
        target = await api.downloadJobMedia(this._jobId, 'result', this._jobId ? '' : this.data.resultUrl);
        this._mediaCache = this._mediaCache || {};
        this._mediaCache.result = target;
        update(this,{resultUrl: target, resultError: ''});
      }
      this.saveToAlbum(target, done);
    } catch (err) {
      done();
      wx.showModal({title: '图片下载失败', content: err.message || 'COS 下载失败，请重试', showCancel: false});
    }
  },

  saveToAlbum(filePath, done, retried) {
    wx.saveImageToPhotosAlbum({
      filePath: filePath,
      success: () => {
        done();
        wx.showToast({ title: '已保存至相册', icon: 'success' });
      },
      fail: (err) => {
        const msg = (err && err.errMsg) || '';
        if (!retried && /suffix|extension|后缀|format/i.test(msg) && !/\.jpg$/i.test(filePath)) {
          const fsm = wx.getFileSystemManager();
          const fixed = wx.env.USER_DATA_PATH + '/rescue_' + Date.now() + '.jpg';
          try {
            fsm.copyFileSync(filePath, fixed);
            this.saveToAlbum(fixed, () => {
              try { fsm.unlinkSync(fixed); } catch (e) {}
              done();
            }, true);
            return;
          } catch (e) {}
        }
        if (!retried && this._jobId && /not exist|no such file|file.*missing|文件不存在/i.test(msg)) {
          // 微信可能清理后台页面的临时文件；重新从 COS 下载一次，不走图片代理。
          api.downloadJobMedia(this._jobId, 'result', '').then((path) => {
            this._mediaCache = this._mediaCache || {};
            this._mediaCache.result = path;
            update(this,{resultUrl: path, resultError: ''});
            this.saveToAlbum(path, done, true);
          }).catch((e) => {
            done();wx.showModal({title: '图片下载失败', content: e.message || 'COS 下载失败', showCancel: false});
          });
          return;
        }
        done();
        if (/auth deny|auth denied|authorize|permission|scope.writePhotosAlbum|权限/i.test(msg)) {
          wx.showModal({
            title: '需要相册权限',
            content: '请允许访问您的相册以便保存高清修复照片。',
            confirmText: '前往授权',
            success: (r) => { if (r.confirm) wx.openSetting(); }
          });
        } else if (msg.indexOf('cancel') < 0) {
          wx.showModal({title: '相册保存失败', content: msg || '本地图片保存失败，请重试', showCancel: false});
        }
      }
    });
  },

  onShareAppMessage() {
    return {
      title: '看我用 AI 拯救的照片，画质太惊艳了！',
      path: '/pages/index/index'
    };
  }
});
