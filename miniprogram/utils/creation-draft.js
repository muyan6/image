const api = require('./api.js');
const DRAFT_KEY = 'creationDraftV1';
const PENDING_KEY = 'creationSubmissionV1';
let saves = Promise.resolve();

function owner() { const app = getApp(); return (app.globalData && app.globalData.userId) || ''; }
function read(key) {
  try {
    const value = wx.getStorageSync(key);
    if (!value || typeof value !== 'object' || value.owner !== owner()) return null;
    return value;
  } catch (e) { return null; }
}
function exists(path) {
  if (!path) return false;
  try { wx.getFileSystemManager().accessSync(path); return true; } catch (e) { return false; }
}
function ownedImage(path) {
  if(typeof path!=='string'||!wx.env||!wx.env.USER_DATA_PATH)return false;
  const prefix=wx.env.USER_DATA_PATH+'/';
  return path.indexOf(prefix)===0 && /^creation_draft_[0-9]+_[a-z0-9]+\.jpg$/.test(path.slice(prefix.length));
}
function removeImage(path) {
  if (!ownedImage(path)) return;
  try { wx.getFileSystemManager().unlinkSync(path); } catch (e) {}
}
function readDraft() {
  const value = read(DRAFT_KEY);
  if (!value) return null;
  const draft = Object.assign({}, value);
  if (draft.input_mode === 'photo' && !exists(draft.imagePath)) {
    draft.imagePath = ''; draft.imageMissing = true;
  }
  return draft;
}
function saveDraft(value) {
  const snapshot = Object.assign({}, value, {owner: owner(), updated_at: Date.now(), version: 1});
  delete snapshot.rightsOk; // 每次恢复照片草稿都重新确认授权。
  const save = async () => {
    const pending=readPendingSubmission();
    if(pending&&pending.input_mode!==snapshot.input_mode)throw new Error('上次提交仍待确认，已保留原创作草稿');
    let previous;
    try {previous=wx.getStorageSync(DRAFT_KEY);}catch(e){}
    let image = snapshot.imagePath || '';
    if (snapshot.input_mode === 'photo' && image && !ownedImage(image)) {
      if (previous && previous.owner===snapshot.owner && previous.sourceImagePath === image && exists(previous.imagePath)) {snapshot.sourceImagePath=previous.sourceImagePath;image = previous.imagePath;}
      else {
        const target = wx.env.USER_DATA_PATH + '/creation_draft_' + Date.now() + '_' + Math.random().toString(36).slice(2, 8) + '.jpg';
        try {
          await new Promise((resolve, reject) => wx.getFileSystemManager().copyFile({srcPath:image,destPath:target,success:resolve,fail:reject}));
          snapshot.sourceImagePath = image; image = target;
        } catch (e) { removeImage(target);image = ''; snapshot.imageMissing = true; }
      }
    }
    if(ownedImage(image)&&!exists(image))image='';
    snapshot.imagePath = snapshot.input_mode === 'photo' ? image : '';
    snapshot.imageMissing = snapshot.input_mode === 'photo' && !image;
    try { wx.setStorageSync(DRAFT_KEY, snapshot); }
    catch (e) { if (image !== (previous && previous.imagePath)) removeImage(image); throw new Error('草稿保存未完成，请稍后重试'); }
    if (previous && previous.imagePath !== image) removeImage(previous.imagePath);
    return snapshot;
  };
  const result = saves.then(save, save);
  saves = result.catch(() => {});
  return result;
}
function clearDraft() {
  const clear = () => { const previous=read(DRAFT_KEY); wx.removeStorageSync(DRAFT_KEY); if(previous)removeImage(previous.imagePath); };
  const result=saves.then(clear,clear);saves=result.catch(()=>{});return result;
}
function navigate(url) {
  return new Promise((resolve, reject) => wx.navigateTo({url,success:()=>resolve(true),fail:()=>reject(new Error('创作页打开失败，请重试'))}));
}
function resumeDraft() {
  const draft = readDraft();
  if (!draft) return Promise.resolve(false);
  if (draft.input_mode === 'text') return navigate('/pages/text-generation/text-generation?restoreDraft=1');
  return navigate('/pages/adjust/adjust?restoreDraft=1');
}
async function recreateFromJob(jobId) {
  if(readPendingSubmission())throw new Error('还有上次提交待确认，请先核对后再创作');
  if (!jobId) throw new Error('缺少作品信息，请重新打开作品');
  await api.ensureLogin();
  const requestOwner=owner();
  const recipe=await api.request('/api/jobs/'+encodeURIComponent(jobId)+'/recipe');
  if(owner()!==requestOwner)throw new Error('登录状态已变化，请重新打开作品');
  if (!recipe || !(recipe.recipe_complete === true || recipe.complete === true)) throw new Error('这份旧作品的创作参数不完整，请重新选择照片或填写描述');
  const value=Object.assign({},recipe,{source_job_id:jobId,show_custom_prompt:!!recipe.custom_prompt});
  if (recipe.input_mode === 'text') {
    if (!(recipe.prompt || '').trim()) throw new Error('这份旧作品未保存描述，请重新填写');
  } else {
    if (!recipe.orig_url) throw new Error('原图已到保存期限，请重新选择照片');
    value.imagePath=await api.downloadRecipeOriginal(jobId,recipe.orig_url);
    if(owner()!==requestOwner)throw new Error('登录状态已变化，请重新打开作品');
  }
  const draft=await saveDraft(value);
  if (draft.imageMissing) throw new Error('原图草稿未保存，参数已保留，请重新选择照片');
  return resumeDraft();
}
function readPendingSubmission() { return read(PENDING_KEY); }
function beginSubmission(inputMode, recipe) {
  const old=readPendingSubmission();
  if (old) return old;
  const pending={owner:owner(),input_mode:inputMode,client_request_id:'cr_'+Date.now().toString(36)+'_'+Math.random().toString(36).slice(2)+Math.random().toString(36).slice(2),recipe,created_at:Date.now(),retryable:false};
  try { wx.setStorageSync(PENDING_KEY,pending); } catch(e) { throw new Error('提交记录保存失败，请重试；本次尚未提交'); }
  return pending;
}
function markSubmissionRetryable() { const pending=readPendingSubmission();if(pending){pending.retryable=true;wx.setStorageSync(PENDING_KEY,pending);}return pending; }
function updateRetryableSubmission(options) {
  const pending=readPendingSubmission();if(!pending||!pending.retryable)return pending;
  if(Number.isInteger(options.expected_price)) {
    if(pending.input_mode==='photo')pending.recipe.form.expected_price=options.expected_price;
    else pending.recipe.expected_price=options.expected_price;
  }
  if(options.imagePath&&pending.input_mode==='photo')pending.recipe.draft=Object.assign({},pending.recipe.draft,{imagePath:options.imagePath,imageMissing:false});
  wx.setStorageSync(PENDING_KEY,pending);return pending;
}
function resolveSubmission(requestId) { const pending=readPendingSubmission();if(pending&&(!requestId||requestId===pending.client_request_id))wx.removeStorageSync(PENDING_KEY); }
async function reconcileSubmission() {
  const pending=readPendingSubmission();if(!pending)return null;
  const requestOwner=owner();
  try {
    const response=typeof api.lookupSubmission==='function'?await api.lookupSubmission(pending.client_request_id):await api.request('/api/me/submissions/'+encodeURIComponent(pending.client_request_id));
    if(owner()!==requestOwner)throw new Error('登录状态已变化，请重新核对提交');
    const state=response&&(response.state||response.submission_state);
    if (state==='accepted'&&response.job_id) resolveSubmission(pending.client_request_id);
    else if(state==='rejected')resolveSubmission(pending.client_request_id);
    return Object.assign({},response,{pending});
  } catch(e) {
    if(e.status===404){markSubmissionRetryable();return {state:'not_found',pending};}
    throw e;
  }
}
module.exports={readDraft,saveDraft,clearDraft,resumeDraft,recreateFromJob,readPendingSubmission,beginSubmission,markSubmissionRetryable,updateRetryableSubmission,resolveSubmission,reconcileSubmission};
