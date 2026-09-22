const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const {JSDOM} = require('jsdom');
const root = path.join(__dirname, '..');
const settle = () => new Promise(resolve => setImmediate(resolve));

async function studio(studioOptions={}) {
  const dom = new JSDOM(fs.readFileSync(path.join(root,'ui/image_studio.html'),'utf8'),
    {url:'http://localhost/image', runScripts:'outside-only'});
  const w = dom.window, requests=[];
  w.setInterval = () => 0;
  w.HTMLElement.prototype.scrollIntoView = () => {};
  w.HTMLCanvasElement.prototype.getContext = () => ({clearRect(){}});
  const photos=[{id:'image_'+'a'.repeat(32),name:'fixture.png',prompt:'A blue square',created_at:1,
    file_url:'/api/images/files/fixture.png',content_type:'image/png',width:256,height:256,dwm_scale:0.5}];
  let trashed=false;
  w.fetch = async (url, options={}) => {
    const payload=options.body?JSON.parse(options.body):null;
    requests.push({url,payload});
    let data={};
    if(url==='/api/images/health')data=studioOptions.health||{ready:true,dwm:{enabled:true},dwm_default_scale:0.75,prompt_enhancement:{t2i:true,edit:true}};
    else if(url==='/api/workspaces')data={image:true,video:true};
    else if(url==='/api/images/gallery')data={ok:true,count:trashed?0:1,items:trashed?[]:photos,trash_count:trashed?1:0};
    else if(url==='/api/images/gallery/trash'&&payload){trashed=true;data={ok:true,results:[{ok:true,name:'fixture.png',trash_id:'b'.repeat(32)}]};}
    else if(url==='/api/images/gallery/trash')data={ok:true,items:trashed?[{id:'b'.repeat(32),name:'fixture.png',deleted_at:1,state:'trashed'}]:[]};
    else if(url==='/api/images/gallery/restore'){trashed=false;data={ok:true,results:[{ok:true,name:'fixture.png'}]};}
    else if(url==='/api/images/jobs')data={id:'img_'+'c'.repeat(24)};
    else if(url.startsWith('/api/images/jobs/'))data={status:'done',stage:'Complete',created_at:Date.now()/1000,seconds:1,outputs:[]};
    return {ok:true,json:async()=>data,blob:async()=>new w.Blob(['fixture'],{type:'image/png'})};
  };
  new vm.Script(fs.readFileSync(path.join(root,'ui/image_studio.js'),'utf8')).runInContext(dom.getInternalVMContext());
  // File decoding is exercised with real PNGs by Python and native browser checks.
  // These DOM tests focus on user actions, ordering and the actual submitted payload.
  w.eval('readReference=async file=>({name:file.name,data:"data:"+file.name,width:256,height:256})');
  await settle();
  return {w,requests,close:()=>dom.window.close()};
}

test('fresh form accepts ten references and submits them in reordered sequence', async()=>{
  const {w,requests,close}=await studio();
  try {
    assert.equal(w.document.querySelector('#references-section').hidden,false);
    assert.equal(w.document.querySelector('#video-workspace').hidden,false);
    assert.equal(w.document.querySelector('#dwm-scale').value,'0.75');
    await w.eval('addFiles(Array.from({length:10},(_,i)=>({name:"ref"+i+".png"})))');
    assert.equal(w.document.querySelector('#reference-count').textContent,'10 / 10');
    w.document.querySelector('[aria-label="Move reference right 1"]').click();
    w.document.querySelector('#prompt').value='Arrange these objects on a table';
    w.document.querySelector('#auto-enhance').checked=false;
    await w.eval('generate()');
    const request=requests.find(r=>r.url==='/api/images/jobs').payload;
    assert.equal(request.mode,'auto');
    assert.equal(request.images_b64.length,10);
    assert.deepEqual(request.images_b64.slice(0,2),['data:ref1.png','data:ref0.png']);
    assert.equal(request.dwm_scale,0.75);
    await w.eval('addFiles([{name:"overflow.png"}])');
    assert.match(w.document.querySelector('#form-error').textContent,/up to 10/);
    assert.equal(w.eval('state.refs.length'),10);
    w.document.querySelector('#clear-references').click();
    assert.equal(w.eval('state.refs.length'),0);
    assert.equal(w.document.querySelector('#generate span').textContent,'Generate image');
  } finally {close();}
});

test('concurrent additions remain bounded and gallery reuse appends',async()=>{
  const {w,close}=await studio();
  try {
    await w.eval('addFiles([{name:"original.png"}])');
    await w.eval('useAsReference("fixture.png")');
    assert.deepEqual(Array.from(w.eval('state.refs.map(r=>r.name)')),['original.png','fixture.png']);
    await w.eval('Promise.all([addFiles(Array.from({length:5},(_,i)=>({name:"a"+i}))),addFiles(Array.from({length:5},(_,i)=>({name:"b"+i})))])');
    assert.equal(w.eval('state.refs.length'),7);
    w.eval('setMode("masked")');
    assert.equal(w.document.querySelector('#reference-count').textContent,'7 / 9');
    assert.match(w.document.querySelector('#reference-hint').textContent,/9 references/);
  } finally {close();}
});

test('gallery delete, Undo, Trash view and restore use backend IDs',async()=>{
  const {w,requests,close}=await studio();
  try {
    await w.eval('trashImages([galleryItems[0].id])');
    assert.equal(w.document.querySelector('#gallery-count').textContent,'0');
    assert.equal(w.document.querySelector('#undo-trash').hidden,false);
    await w.document.querySelector('#undo-trash').onclick();
    assert.equal(w.document.querySelector('#gallery-count').textContent,'1');
    const deletion=requests.find(r=>r.url==='/api/images/gallery/trash'&&r.payload);
    assert.deepEqual(deletion.payload.ids,['image_'+'a'.repeat(32)]);
    assert.equal(requests.filter(r=>r.url==='/api/images/gallery/restore').length,1);
    await w.eval('trashImages([galleryItems[0].id])');
    w.document.querySelector('#show-trash').click();await settle();
    assert.equal(w.document.querySelector('#restore-selected').hidden,false);
    assert.match(w.document.querySelector('#gallery').textContent,/Restore/);
  } finally {close();}
});

test('basic Comfy image capabilities disable Qwen-only modes and bound references',async()=>{
  const health={ready:true,capabilities:['text_to_image','image_edit'],controls:{max_references:1,
    resolutions:[384,512,1024,2048],resolution_default:384,edit_reference_resolution:384,
    steps:{min:1,max:200,default:20}},
    dwm:{enabled:false},dwm_default_scale:0,prompt_enhancement:{t2i:false,edit:false}};
  const {w,requests,close}=await studio({health});
  try {
    await w.eval('refreshHealth()');
    for(const mode of ['transparent','extract','masked','annotate'])
      assert.equal(w.document.querySelector(`[data-mode="${mode}"]`).hidden,true,
        JSON.stringify({mode,capabilities:Array.from(w.eval('state.capabilities')),health:w.document.querySelector('#health').textContent}));
    assert.equal(w.document.querySelector('#auto-enhance').checked,false);
    assert.equal(w.document.querySelector('#auto-enhance').disabled,true);
    assert.equal(w.document.querySelector('#reference-count').textContent,'0 / 1');
    assert.equal(w.document.querySelector('#resolution').value,'384');
    assert.equal(w.document.querySelector('#steps').value,'20');
    await w.eval('addFiles([{name:"source.png"}])');
    assert.equal(w.document.querySelector('#resolution').disabled,true);
    assert.equal(w.document.querySelector('#custom-size').disabled,true);
    assert.equal(w.document.querySelector('#dimensions').textContent,'Reference-derived · detail 384');
    await w.eval('addFiles([{name:"overflow.png"}])');
    assert.equal(w.eval('state.refs.length'),1);
    assert.match(w.document.querySelector('#form-error').textContent,/up to 1/);
    w.document.querySelector('#prompt').value='Turn the source into a watercolor painting';
    await w.eval('generate()');
    const request=requests.find(r=>r.url==='/api/images/jobs').payload;
    assert.equal(request.mode,'auto');
    assert.deepEqual(request.images_b64,['data:source.png']);
    for(const field of ['enhance_prompt','auto_aspect_ratio','dwm_scale','use_kv_cache','reference_resolution','preserve_unmasked'])
      assert.equal(field in request,false);
  } finally {close();}
});

test('Comfy masked mode keeps its separate one-reference allowance',async()=>{
  const health={ready:true,
    capabilities:['text_to_image','image_edit','transparent_png','transparent_edit','subject_extraction','mask_edit','annotation_edit'],
    controls:{max_references:1,max_references_by_mode:{masked:1},resolutions:[384],resolution_default:384,
      edit_reference_resolution:384,steps:{min:1,max:200,default:20}},
    dwm:{enabled:false},dwm_default_scale:0,prompt_enhancement:{t2i:false,edit:false}};
  const {w,close}=await studio({health});
  try {
    await w.eval('refreshHealth()');
    w.document.querySelector('[data-mode="masked"]').click();
    assert.equal(w.document.querySelector('#reference-count').textContent,'0 / 1');
    assert.match(w.document.querySelector('#reference-hint').textContent,/1 references \+ 1 mask/);
    await w.eval('addFiles([{name:"canvas.png"}])');
    assert.equal(w.document.querySelector('#reference-count').textContent,'1 / 1');
  } finally {close();}
});

test('native image controls retain the 1K and 40-step defaults',async()=>{
  const health={ready:true,controls:{max_references:10,resolutions:[512,1024,2048],
    resolution_default:1024,steps:{min:1,max:80,default:40}},
    dwm:{enabled:false},prompt_enhancement:{t2i:true,edit:true}};
  const {w,close}=await studio({health});
  try {
    await w.eval('refreshHealth()');
    assert.deepEqual(Array.from(w.document.querySelector('#resolution').options).map(option=>option.value),['512','1024','2048']);
    assert.equal(w.document.querySelector('#resolution').value,'1024');
    assert.equal(w.document.querySelector('#steps').value,'40');
  } finally {close();}
});

test('multi-reference Comfy capability does not send Qwen-only controls',async()=>{
  const health={ready:true,capabilities:['text_to_image','image_edit','multi_reference'],controls:{max_references:2},
    dwm:{enabled:false},prompt_enhancement:{t2i:false,edit:false}};
  const {w,requests,close}=await studio({health});
  try {
    await w.eval('refreshHealth()');
    await w.eval('addFiles([{name:"first.png"},{name:"second.png"}])');
    w.document.querySelector('#prompt').value='Combine both references';
    await w.eval('generate()');
    const request=requests.find(r=>r.url==='/api/images/jobs').payload;
    assert.equal(request.images_b64.length,2);
    assert.equal('use_kv_cache' in request,false);
    assert.equal('reference_resolution' in request,false);
  } finally {close();}
});
