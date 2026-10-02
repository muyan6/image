import {createApplication} from './app.js';
const application=createApplication();
application.start().catch(()=>application.ctx.toast('页面准备尚未完成，请刷新重试。'));
window.addEventListener('pagehide',()=>application.destroy(),{once:true});
