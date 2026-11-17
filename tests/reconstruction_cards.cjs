'use strict';
const assert=require('node:assert/strict');
const path=require('node:path');

class Node {
 constructor(tag,doc){this.tagName=tag;this.doc=doc;this.children=[];this.dataset={};this.style={};this.textContent='';this.value='';}
 append(...nodes){this.children.push(...nodes);}
 replaceChildren(...nodes){this.children=nodes;}
 setAttribute(key,value){this[key]=value;}
 querySelectorAll(){return [];}
}
const card={sections:[{title:'BallSprite showTrail',
 original:{label:'Original Thumb instructions',source:'libgame.so',locator:'BallSprite.showTrail +0x0',source_sha256:'abc123',code:'push {r4, lr}'},
 reconstruction:{label:'Browser translation',source:'web/ball-trail-visibility.js',code:'streak.setVisible(true);'},
 period_hypothesis:'A 2012 Cocos2d game would normally retain the streak node and toggle visibility.',
 comparison:{status:'Exact differential match',scope:'ten controlled service-call traces',result:'receiver, BOOL and order match'},
 missing:[{identifier:'trailTexture',question:'Which original atlas frame initializes the streak?',unblocks:'visual diff'}]}]};
const context={task:{id:'task',project_id:'project',record_revision:1,title:'Trail visibility',status:'review'},
 acceptance_state:'pending',progress:{claim:{state:'unclaimed'}},history:[{details:{text:'Reconstruction v1\n'+JSON.stringify(card)}}]};
const state={projectId:'project',principalId:'author',taskId:'task',revision:'store:1',context,
 capabilities:{authenticated:true,granted:['contribute']},pages:{},draft:null,phase:'ready',busy:false,pendingBody:null};
global.WorkspaceTaskSession={createSession(){return{state:()=>state};}};
const tasks=require(process.env.WORKSPACE_TASK_WORKFLOWS||path.resolve(__dirname,'../docs/workspace-task-workflows.js'));
const doc={activeElement:null,createElement(tag){return new Node(tag,this);}},mount=new Node('main',doc);
tasks.create({mount,document:doc,getContext:()=>({projectId:'project',principalId:'author',taskId:'task'}),storage:{}});
const all=node=>[node,...node.children.filter(child=>child&&typeof child==='object').flatMap(all)];
assert.ok(all(mount).some(node=>node.tagName==='h5'&&node.textContent==='Original Thumb instructions'),'original instructions have a distinct pane');
assert.ok(all(mount).some(node=>node.tagName==='code'&&node.textContent==='push {r4, lr}'),'machine instructions render as inert text');
assert.ok(all(mount).some(node=>node.tagName==='h5'&&node.textContent==='Browser translation'),'reconstruction has a distinct pane');
assert.ok(all(mount).some(node=>node.tagName==='p'&&/2012 Cocos2d/.test(node.textContent)),'historical practice stays visibly labeled as hypothesis');
assert.ok(all(mount).some(node=>node.tagName==='p'&&/trailTexture/.test(node.textContent)&&/visual diff/.test(node.textContent)),'missing variable watch appears beside the comparison');
delete global.WorkspaceTaskSession;
console.log('paired reconstruction card checks passed');
