const app=getApp();
const api=require('../../utils/api.js');
const update=(page,patch)=>typeof api.setDataStable==='function'?api.setDataStable(page,patch):page.setData(patch);
const reportReasons=[['spam','广告引流'],['abuse','辱骂攻击'],['inappropriate','不适宜内容'],['privacy','侵犯隐私'],['copyright','侵权'],['other','其他问题']];
const dateLabel=at=>{const d=new Date(at*1000),pad=x=>String(x).padStart(2,'0');return `${d.getFullYear()}.${pad(d.getMonth()+1)}.${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`;};

Page({
  data:{post:null,loading:true,error:'',unavailable:false,comments:[],commentTotal:0,hasMore:false,commentsLoading:false,
    commentError:'',draft:'',sending:false,sendError:''},
  onLoad(options){
    this._id=options.id||'';
    const preview=app.globalData.communityPreview;
    if(preview&&preview.id===this._id)update(this,{post:preview});
    return this.loadPost();
  },
  onUnload(){this._unloaded=true;},
  onPullDownRefresh(){return this.loadPost().finally(()=>wx.stopPullDownRefresh());},
  async loadPost(){
    if(this._postLoading)return;this._postLoading=true;update(this,{loading:!this.data.post,error:''});
    try{
      const d=await api.request('/api/community/posts/'+encodeURIComponent(this._id));
      if(this._unloaded)return;
      const p=d.post,prior=this.data.post,display=(url,old)=>typeof api.stableImageUrl==='function'?api.stableImageUrl(url,old):(typeof api.communityImage==='function'?api.communityImage(url):api.absolute(url));
      update(this,{post:{...p,resultRemote:api.absolute(p.resultUrl),origRemote:api.absolute(p.origUrl),
        resultUrl:display(p.resultUrl,prior&&prior.resultUrl),origUrl:display(p.origUrl,prior&&prior.origUrl),authorAvatar:p.authorAvatar?api.absolute(p.authorAvatar):''},unavailable:false});
      await this.loadComments();
    }catch(e){if(!this._unloaded){
      update(this,{error:e.message||'作品读取失败',...(e.status===404?{post:null,comments:[],unavailable:true}:{} )});
      if(e.status===404){app.globalData.communityDirty=true;app.globalData.communityPreview=null;}
    }}finally{this._postLoading=false;if(!this._unloaded)update(this,{loading:false});}
  },
  loadComments(more=false){
    more=more===true;if(this._unloaded||!this.data.post||(more&&!this.data.hasMore))return Promise.resolve();
    if(this._commentsPromise){
      // Publish/delete/refresh must follow an older snapshot, not be discarded.
      if(!more)this._commentsRefreshQueued=true;
      return this._commentsPromise;
    }
    this._commentsBusy=true;update(this,{commentsLoading:true,commentError:''});
    this._commentsPromise=(async()=>{
      let append=more;
      do{
        this._commentsRefreshQueued=false;
        const offset=append?(this._offset||0):0;
        let d;
        try{d=await api.request('/api/community/posts/'+encodeURIComponent(this._id)+'/comments?limit=30&offset='+offset);}
        catch(e){if(!this._commentsRefreshQueued)throw e;}
        if(this._unloaded)return;
        // Do not publish a stale count/list when a mutation already queued a refresh.
        if(!this._commentsRefreshQueued){
          const items=(d.items||[]).map(x=>({...x,dateLabel:dateLabel(x.created_at)}));
          this._offset=d.next_offset;
          update(this,{comments:append?[...new Map([...this.data.comments,...items].map(x=>[x.id,x])).values()]:items,
            commentTotal:d.total,hasMore:!!d.has_more});this.publishDelta({comments:d.total});
        }
        append=false;
      }while(this._commentsRefreshQueued&&this.data.post);
    })().catch(e=>{if(!this._unloaded)update(this,{commentError:e.message||'留言读取失败'});})
      .finally(()=>{this._commentsPromise=null;this._commentsBusy=false;if(!this._unloaded)update(this,{commentsLoading:false});});
    return this._commentsPromise;
  },
  onRetry(){return this.loadPost();},
  onRetryComments(){return this.loadComments();},
  onMoreComments(){return this.loadComments(true);},
  onDraft(e){update(this,{draft:e.detail.value,sendError:''});},
  async onSend(){
    if(this.data.sending||!this.data.post)return;
    const content=this.data.draft.trim();if(!content){wx.showToast({title:'请写下留言',icon:'none'});return;}
    if(!this._ticket||this._ticket.content!==content)this._ticket={content,id:'comment_'+Date.now().toString(36)+'_'+Math.random().toString(36).slice(2,12)};
    update(this,{sending:true,sendError:''});
    try{
      await api.request('/api/community/posts/'+encodeURIComponent(this._id)+'/comments',{method:'POST',data:{content,request_id:this._ticket.id}});
      if(this._unloaded)return;
      this._ticket=null;update(this,{draft:''});wx.hideKeyboard();
      wx.showToast({title:'微信审核通过，留言已发布',icon:'none'});await this.loadComments();
    }catch(e){if(!this._unloaded){update(this,{sendError:e.message||'留言暂未发布，请重试'});if(e.status===400)this._ticket=null;}}
    finally{if(!this._unloaded)update(this,{sending:false});}
  },
  publishDelta(delta){
    app.globalData.communityUpdated={id:this._id,...(app.globalData.communityUpdated&&app.globalData.communityUpdated.id===this._id?app.globalData.communityUpdated:{}),...delta};
    update(this,{post:{...this.data.post,...delta}});
  },
  async onLikePost(){
    if(this._likePost||!this.data.post)return;this._likePost=true;
    try{const d=await api.request('/api/community/posts/'+encodeURIComponent(this._id)+'/like',{method:'PUT',data:{liked:!this.data.post.liked}});
      if(!this._unloaded)this.publishDelta({likes:d.likes,liked:d.liked});
    }catch(e){if(!this._unloaded)wx.showToast({title:e.message||'点赞暂未保存',icon:'none'});}
    finally{this._likePost=false;}
  },
  async onLikeComment(e){
    const id=e.currentTarget.dataset.id,row=this.data.comments.find(x=>x.id===id);if(!row)return;
    this._likes=this._likes||new Set();if(this._likes.has(id))return;this._likes.add(id);
    try{const d=await api.request('/api/community/comments/'+encodeURIComponent(id)+'/like',{method:'PUT',data:{liked:!row.liked}});
      if(!this._unloaded)update(this,{comments:this.data.comments.map(x=>x.id===id?{...x,likes:d.likes,liked:d.liked}:x)});
    }catch(err){if(!this._unloaded)wx.showToast({title:err.message||'点赞暂未保存',icon:'none'});}
    finally{this._likes.delete(id);}
  },
  onReport(e){
    const kind=e.currentTarget.dataset.kind,id=kind==='post'?this._id:e.currentTarget.dataset.id;
    if(this._reportBusy)return;
    wx.showActionSheet({itemList:reportReasons.map(x=>x[1]),success:async r=>{
      if(this._reportBusy||this._unloaded)return;this._reportBusy=true;
      try{await api.request('/api/community/'+(kind==='post'?'posts/':'comments/')+encodeURIComponent(id)+'/report',
        {method:'POST',data:{reason:reportReasons[r.tapIndex][0]}});
        if(!this._unloaded)wx.showToast({title:'举报已收到，谢谢你的反馈',icon:'none'});
      }catch(err){if(!this._unloaded)wx.showToast({title:err.message||'举报暂未提交',icon:'none'});}
      finally{this._reportBusy=false;}
    }});
  },
  onDeleteComment(e){
    const id=e.currentTarget.dataset.id;
    wx.showModal({title:'删除留言',content:'删除后不再展示这条留言。',confirmText:'删除',confirmColor:'#9e4b3c',success:async r=>{
      if(!r.confirm||this._deleting)return;this._deleting=true;
      try{await api.request('/api/community/comments/'+encodeURIComponent(id),{method:'DELETE'});if(!this._unloaded)await this.loadComments();}
      catch(err){if(!this._unloaded)wx.showToast({title:err.message||'删除暂未完成',icon:'none'});}
      finally{this._deleting=false;}
    }});
  },
  onImageLoad(e){const kind=e.currentTarget.dataset.kind;if(typeof api.rememberCommunityImage==='function'&&this.data.post)api.rememberCommunityImage(this.data.post[kind+'Remote']).catch(()=>{});},
  onImageError(e){
    const kind=e.currentTarget.dataset.kind;if(!this.data.post)return;
    if(typeof api.forgetCommunityImage==='function')api.forgetCommunityImage(this.data.post[kind+'Remote']);
    this._imageRetries=this._imageRetries||new Set();if(this._imageRetries.has(kind))return;
    this._imageRetries.add(kind);this.loadPost();
  },
  onPreview(e){const p=this.data.post;if(!p)return;const urls=[p.resultUrl,p.origUrl].filter(Boolean);wx.previewImage({urls,current:e.currentTarget.dataset.kind==='orig'?p.origUrl:p.resultUrl});},
  onMakeSame(){const p=this.data.post;if(!p||!p.templateId)return;
    app.globalData.selectedTemplate={id:p.templateId,name:p.templateName};wx.switchTab({url:'/pages/index/index'});
  },
  onShareAppMessage(){const p=this.data.post;return {title:p?p.title:'来灵感沙龙看看大家的作品',
    path:'/pages/community-detail/community-detail?id='+encodeURIComponent(this._id),imageUrl:p?(p.resultRemote||p.resultUrl):'/images/logo.jpg'};}
});
