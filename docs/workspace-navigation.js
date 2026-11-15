/* Share a selected project across workbench pages, never across service origins. */
(function(root){
'use strict';
const KEY='workbench.selected-project.v1';
function create(storage){
 function remembered(){try{const value=storage?.getItem(KEY);return typeof value==='string'&&value.length<=200?value:null}catch{return null}}
 return{choose(items,preferred=null){const ids=(items||[]).map(item=>item.id);if(ids.includes(preferred))return preferred;const previous=remembered();return ids.includes(previous)?previous:ids[0]||null},remember(id){if(typeof id!=='string'||!id||id.length>200)return;try{storage?.setItem(KEY,id)}catch{}}};
}
function browser(){let storage=null;try{storage=root.sessionStorage}catch{}return create(storage)}
const exported={create,browser};root.WorkspaceNavigation=exported;if(typeof module!=='undefined'&&module.exports)module.exports=exported;
})(typeof window!=='undefined'?window:globalThis);
