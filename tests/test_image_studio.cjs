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
    file_url:'/api/images/files/fixture.png',content_type:'image/png',width:256,height:256,seed:42,seconds:12.34,dwm_scale:0.5}];
  let trashed=false,purged=false;
  w.fetch = async (url, options={}) => {
    const payload=options.body?JSON.parse(options.body):null;
    requests.push({url,payload});
    let data={};
    if(url==='/api/images/health')data=studioOptions.health||{ready:true,dwm:{enabled:true},dwm_default_scale:0.75,prompt_enhancement:{t2i:true,edit:true},controls:{max_references:16,max_references_by_mode:{masked:15}}};
    else if(url==='/api/workspaces')data={image:true,video:true};
    else if(url==='/api/images/gallery')data={ok:true,count:trashed||purged?0:1,items:trashed||purged?[]:photos,trash_count:trashed&&!purged?1:0};
    else if(url==='/api/images/gallery/trash'&&payload){trashed=true;data={ok:true,results:[{ok:true,name:'fixture.png',trash_id:'b'.repeat(32)}]};}
    else if(url==='/api/images/gallery/trash')data={ok:true,items:trashed&&!purged?[{id:'b'.repeat(32),name:'fixture.png',deleted_at:1,state:'trashed'}]:[]};
    else if(url==='/api/images/gallery/restore'){trashed=false;data={ok:true,results:[{ok:true,name:'fixture.png'}]};}
    else if(url==='/api/images/gallery/purge'){purged=true;data={ok:true,results:[{ok:true,name:'fixture.png',state:'purged'}]};}
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

test('fresh form accepts sixteen references and submits them in reordered sequence', async()=>{
  const {w,requests,close}=await studio();
  try {
    assert.equal(w.document.querySelector('#references-section').hidden,false);
    assert.equal(w.document.querySelector('#video-workspace').hidden,false);
    assert.equal(w.document.querySelector('#dwm-scale').value,'0.75');
    await w.eval('addFiles(Array.from({length:16},(_,i)=>({name:"ref"+i+".png"})))');
    assert.equal(w.document.querySelector('#reference-count').textContent,'16 / 16');
    assert.match(w.document.querySelector('#reference-hint').textContent,/16 references/);
    w.document.querySelector('[aria-label="Move reference right 1"]').click();
    w.document.querySelector('#prompt').value='Arrange these objects on a table';
    w.document.querySelector('#auto-enhance').checked=false;
    await w.eval('generate()');
    const request=requests.find(r=>r.url==='/api/images/jobs').payload;
    assert.equal(request.mode,'auto');
    assert.equal(request.images_b64.length,16);
    assert.deepEqual(request.images_b64.slice(0,2),['data:ref1.png','data:ref0.png']);
    assert.equal(request.dwm_scale,0.75);
    await w.eval('addFiles([{name:"overflow.png"}])');
    assert.match(w.document.querySelector('#form-error').textContent,/up to 16/);
    assert.equal(w.eval('state.refs.length'),16);
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
    assert.equal(w.eval('state.refs.length'),12);
    w.eval('setMode("masked")');
    assert.equal(w.document.querySelector('#reference-count').textContent,'12 / 15');
    assert.match(w.document.querySelector('#reference-hint').textContent,/15 references/);
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
    dwm:{enabled:false},prompt_enhancement:{t2i:true,edit:true},controls:{max_references:16,max_references_by_mode:{masked:15}}};
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

test('Turbo sampling defaults to six and accepts other step counts',async()=>{
  const health={ready:true,capabilities:['text_to_image','image_edit'],controls:{max_references:1,
    resolutions:[384,512],resolution_default:512,
    steps:{min:1,step:1,default:6},
    guidance:{min:1,max:1,step:1,default:1},negative_prompt:false}};
  const {w,requests,close}=await studio({health});
  try {
    await w.eval('refreshHealth()');
    const steps=w.document.querySelector('#steps');
    assert.equal(w.document.querySelector('#resolution').value,'512');
    assert.equal(w.document.querySelector('#dimensions').textContent,'512 × 512');
    assert.equal(steps.value,'6');assert.equal(steps.min,'1');
    assert.equal(steps.max,'');assert.equal(steps.step,'1');
    steps.value='7';assert.equal(steps.checkValidity(),true);
    assert.equal(w.document.querySelector('#guidance').value,'1');
    assert.equal(w.document.querySelector('#guidance').disabled,true);
    assert.equal(w.document.querySelector('#negative').disabled,true);
    w.document.querySelector('#prompt').value='A cup';
    await w.eval('generate()');
    const request=requests.find(r=>r.url==='/api/images/jobs');
    assert.equal(request.payload.num_inference_steps,7);
    assert.equal(request.payload.width,512);assert.equal(request.payload.height,512);
    assert.equal(request.payload.true_cfg_scale,1);
    assert.equal(request.payload.negative_prompt,'');
  } finally {close();}
});

test('only Trash offers permanent deletion, with confirmation and Trash IDs',async()=>{
  const {w,requests,close}=await studio();
  try {
    assert.equal(w.document.querySelector('#purge-selected').hidden,true);
    assert.equal(w.document.querySelector('#delete-selected').hidden,false);
    assert.ok([...w.document.querySelectorAll('#gallery button')].some(b=>b.textContent==='Delete'));
    assert.ok(![...w.document.querySelectorAll('#gallery button')].some(b=>b.textContent==='Permanently delete'));
    w.confirm=()=>{throw new Error('Photos must not offer permanent deletion');};
    await w.eval('purgeImages([galleryItems[0].id])');
    assert.equal(requests.filter(r=>r.url==='/api/images/gallery/purge').length,0);

    await w.eval('trashImages([galleryItems[0].id])');
    w.document.querySelector('#show-trash').click();await settle();
    assert.equal(w.document.querySelector('#purge-selected').hidden,false);
    assert.equal(w.document.querySelector('#delete-selected').hidden,true);
    assert.ok([...w.document.querySelectorAll('#gallery button')].some(b=>b.textContent==='Permanently delete'));
    assert.ok(![...w.document.querySelectorAll('#gallery button')].some(b=>b.textContent==='Delete'));
    w.document.querySelector('#select-all-gallery').checked=true;
    w.document.querySelector('#select-all-gallery').dispatchEvent(new w.Event('change'));
    assert.equal(w.document.querySelector('#purge-selected').disabled,false);
    let message='';w.confirm=text=>{message=text;return false;};
    await w.document.querySelector('#purge-selected').onclick();
    assert.match(message,/cannot be undone/);
    assert.equal(requests.filter(r=>r.url==='/api/images/gallery/purge').length,0);
    assert.equal(w.document.querySelector('#selection-count').textContent,'1 selected');
    w.confirm=()=>true;
    await w.document.querySelector('#purge-selected').onclick();
    const request=requests.find(r=>r.url==='/api/images/gallery/purge');
    assert.deepEqual(request.payload,{source:'trash',ids:['b'.repeat(32)]});
    assert.equal(w.document.querySelector('#gallery-count').textContent,'0');
    assert.equal(w.document.querySelector('#trash-count').textContent,'0');
    assert.equal(w.document.querySelector('#undo-trash').hidden,true);
    assert.match(w.document.querySelector('#toast-message').textContent,/permanently deleted/);
    w.document.querySelector('#show-library').click();await settle();
    assert.equal(w.document.querySelector('#purge-selected').hidden,true);
    assert.equal(w.document.querySelector('#delete-selected').hidden,false);
  } finally {close();}
});


test('Image inputs do not impose old area, step, variation or CFG-pair limits',async()=>{
  const {w,requests,close}=await studio({health:{ready:true,capabilities:['text_to_image','image_edit','multi_reference'],controls:{max_references:null,steps:{min:1,default:40,step:1}}}});
  try {
    await w.eval('addFiles(Array.from({length:17},(_,i)=>({name:"ref"+i})))');
    assert.equal(w.document.querySelector('#reference-count').textContent,'17 references');
    w.document.querySelector('#custom-size').checked=true;
    w.document.querySelector('#custom-width').value='4097';
    w.document.querySelector('#custom-height').value='4096';
    w.document.querySelector('#steps').value='301';
    w.document.querySelector('#count').value='9';
    w.document.querySelector('#guidance').value='20';
    w.document.querySelector('#prompt').value='';
    await w.eval('generate()');
    const request=requests.find(r=>r.url==='/api/images/jobs').payload;
    assert.equal(request.width,4097);assert.equal(request.height,4096);
    assert.equal(request.num_inference_steps,301);assert.equal(request.n,9);
    assert.equal(request.images_b64.length,17);assert.equal(request.true_cfg_scale,20);
    assert.equal(request.prompt,'');assert.equal(request.negative_prompt,'');
  } finally {close();}
});


test('gallery shows saved generation seconds after size and seed, and omits missing values',async()=>{
  const {w,close}=await studio();
  try {
    assert.match(w.document.querySelector('#gallery small').textContent,/256 × 256 · seed 42 · 12.34s · DWM 0.5/);
    await w.eval('galleryItems[0].seconds=null;shownItems=galleryItems;renderGallery()');
    assert.equal(w.document.querySelector('#gallery small').textContent,'256 × 256 · seed 42 · DWM 0.5');
  } finally {close();}
});
