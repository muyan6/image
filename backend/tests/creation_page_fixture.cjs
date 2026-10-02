/* Real draft module with per-page offline storage/file doubles, never live wx. */
const fs=require('fs'),path=require('path'),vm=require('vm');
module.exports=function creationFixture(api,wx,app,root){
  const storage=new Map(),files=new Set();
  const get=wx.getStorageSync,set=wx.setStorageSync,remove=wx.removeStorageSync;
  const isCreation=k=>k==='creationDraftV1'||k==='creationSubmissionV1';
  wx.getStorageSync=k=>isCreation(k)?storage.get(k):(get?get(k):null);
  wx.setStorageSync=(k,v)=>{if(isCreation(k))storage.set(k,JSON.parse(JSON.stringify(v)));else if(set)set(k,v);};
  wx.removeStorageSync=k=>{if(isCreation(k))storage.delete(k);else if(remove)remove(k);};
  wx.env=wx.env||{USER_DATA_PATH:'/fixture-files'};
  const oldFs=wx.getFileSystemManager;
  wx.getFileSystemManager=()=>Object.assign({accessSync:p=>{if(!p)throw new Error('missing');},
    copyFile:o=>{files.add(o.destPath);o.success();},unlinkSync:p=>files.delete(p)},oldFs?oldFs():{});
  api.ensureLogin=api.ensureLogin||(()=>Promise.resolve('fixture'));
  api.me=api.me||(()=>Promise.resolve({balance:app.globalData.lightPoints||200}));
  const module={exports:{}};
  vm.runInNewContext(fs.readFileSync(path.join(root,'miniprogram/utils/creation-draft.js'),'utf8'),
    {module,require:()=>api,wx,getApp:()=>app,Date,Math,Promise,console});
  return module.exports;
};
