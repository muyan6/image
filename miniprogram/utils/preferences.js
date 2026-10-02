const api = require('./api.js');

function normalize(value) {
  const ids = list => [...new Set((Array.isArray(list) ? list : []).filter(id => typeof id === 'string' && id))];
  return {template_favorites: ids(value && value.template_favorites), recent_templates: ids(value && value.recent_templates)};
}

async function load() { return normalize(await api.request('/api/me/preferences')); }
async function setFavorite(templateId, favorite) {
  const result = await api.request('/api/me/preferences', {method:'PUT', data:{template_id:templateId, favorite:!!favorite}});
  return result && Array.isArray(result.template_favorites) ? normalize(result) : load();
}
async function recordRecent(templateId) {
  if (!templateId) return;
  return normalize(await api.request('/api/me/recent-template', {method:'POST', data:{template_id:templateId}}));
}

module.exports = {load, setFavorite, recordRecent, normalize};
