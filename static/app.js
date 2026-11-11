'use strict';

const $ = (id) => document.getElementById(id);
const clamp = (v, lo, hi) => Math.max(lo, Math.min(hi, v));
const defaults = () => ({ width:1000, height:1000, x:.5, y:.55, scale:.75, rotation:0, shadow:{opacity:.22,blur:18,offsetX:0,offsetY:12,spread:1}, light:{brightness:1,contrast:1,saturation:1,warmth:0} });
const state = { tab:'compose', original:null, product:null, background:null, result:null, params:defaults(), splitSource:null, splitAssets:[], splitPackageURL:null, cuts:[], layersSource:null, layers:[], layersPackage:null, projectId:null, projectName:'' };
const images = new Map();
let lastLightPair = '', manualLight = false;
let toastTimer, renderVersion = 0, busyCount = 0, previewResult = false;

function showToast(message, error=false) {
  $('toast').textContent = message; $('toast').classList.toggle('error',error); $('toast').classList.remove('hidden');
  clearTimeout(toastTimer); toastTimer = setTimeout(()=>$('toast').classList.add('hidden'), error?7500:4000);
}
async function api(path, data) {
  const response = await fetch(path, data === undefined ? {} : { method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(data) });
  let result; try { result = await response.json(); } catch { throw new Error('本机服务返回异常，请检查程序是否仍在运行。'); }
  if (!response.ok || result.error) throw new Error(typeof result.error==='string'?result.error:(result.message||'处理失败，请重试。'));
  return result;
}
async function task(label, work) {
  if (busyCount) return;
  busyCount++; $('busy-title').textContent=label; $('busy').classList.remove('hidden');
  try { return await work(); } catch(error) { console.error(error); showToast(error.message||String(error),true); }
  finally { if(--busyCount<=0){busyCount=0;$('busy').classList.add('hidden');} }
}
function assetOf(value) { return value.asset || value; }
function loadImage(asset) {
  const url = typeof asset==='string'?asset:asset.url;
  if (!url) return Promise.reject(new Error('图片地址缺失。'));
  if (!images.has(url)) images.set(url,new Promise((resolve,reject)=>{const im=new Image();im.onload=()=>resolve(im);im.onerror=()=>{images.delete(url);reject(new Error('无法读取本地图片，请确认素材仍然存在。'));};im.src=url;}));
  return images.get(url);
}
function fileData(file) { return new Promise((resolve,reject)=>{const reader=new FileReader();reader.onload=()=>resolve(reader.result);reader.onerror=()=>reject(new Error('无法读取这张图片。'));reader.readAsDataURL(file);}); }
async function uploadFile(file) {
  if (!file || !/^image\/(png|jpeg|webp)$/.test(file.type)) throw new Error('请选择 PNG、JPG 或 WEBP 图片。');
  if(file.size>65*1024*1024) throw new Error('单张图片请控制在 65 MB 以内。');
  return assetOf(await api('/api/upload',{name:file.name,data:await fileData(file)}));
}
async function uploadCanvas(canvas,name) { return assetOf(await api('/api/upload',{name,data:canvas.toDataURL('image/png')})); }
function bindUpload(id, handler) {
  const input=$(id), label=input.closest('label');
  const run=(file)=>task('正在导入图片',async()=>{const asset=await uploadFile(file);await loadImage(asset);await handler(asset);input.value='';});
  input.addEventListener('change',()=>{if(input.files[0])run(input.files[0]);});
  for(const ev of ['dragenter','dragover'])label.addEventListener(ev,e=>{e.preventDefault();label.classList.add('drag-over');});
  for(const ev of ['dragleave','drop'])label.addEventListener(ev,e=>{e.preventDefault();label.classList.remove('drag-over');});
  label.addEventListener('drop',e=>{if(e.dataTransfer.files[0])run(e.dataTransfer.files[0]);});
}
function summary(id,asset) {
  const box=$(id);box.replaceChildren();box.classList.toggle('hidden',!asset);if(!asset)return;
  const im=document.createElement('img');im.src=asset.url;im.alt='';const text=document.createElement('div'),name=document.createElement('strong'),size=document.createElement('span');
  name.textContent=asset.name||'已导入图片';size.textContent=`${asset.width} × ${asset.height} 像素`;text.append(name,size);box.append(im,text);
}
function switchTab(tab) {
  state.tab=tab;document.querySelectorAll('.tab').forEach(button=>{const selected=button.dataset.tab===tab;button.classList.toggle('active',selected);selected?button.setAttribute('aria-current','page'):button.removeAttribute('aria-current');});
  document.querySelectorAll('.tab-panel').forEach(panel=>panel.classList.toggle('active',panel.id===`panel-${tab}`));
  if(tab==='projects')refreshProjects();if(tab==='compose')drawCompose();if(tab==='layers')drawLayers();
}
document.querySelectorAll('.tab').forEach(button=>button.addEventListener('click',()=>switchTab(button.dataset.tab)));

function drawCover(ctx, im, w, h) { const f=Math.max(w/im.width,h/im.height),sw=w/f,sh=h/f;ctx.drawImage(im,(im.width-sw)/2,(im.height-sh)/2,sw,sh,0,0,w,h); }
function fitCanvas(canvas,w,h) {
  const box=canvas.parentElement, maxW=Math.max(150,box.clientWidth-42), maxH=Math.max(180,box.clientHeight-42);
  const ratio=Math.min(maxW/w,maxH/h,1);canvas.style.width=`${Math.round(w*ratio)}px`;canvas.style.height=`${Math.round(h*ratio)}px`;
}
function validateDimensions() {
  const w=Number($('output-width').value),h=Number($('output-height').value);
  if(!Number.isInteger(w)||!Number.isInteger(h)||w<256||h<256||w>6000||h>6000||w*h>24000000) throw new Error('画布宽高为 256–6000 像素，总像素请不超过 2400 万。');
  state.params.width=w;state.params.height=h;
}
function markComposeChanged() { previewResult=false;state.result=null;$('download-composite').classList.add('hidden');$('preview-kind').textContent='可拖动产品调整位置 · 预览光影以生成结果为准';drawCompose(); }
async function drawCompose() {
  const token=++renderVersion, c=$('compose-canvas'),p=state.params;
  const factor=Math.min(900/p.width,900/p.height,1),w=Math.max(1,Math.round(p.width*factor)),h=Math.max(1,Math.round(p.height*factor));
  c.width=w;c.height=h;fitCanvas(c,w,h);const ctx=c.getContext('2d');ctx.fillStyle='#fff';ctx.fillRect(0,0,w,h);
  $('compose-empty').classList.toggle('hidden',!!state.product);$('auto-cutout').disabled=!state.original;$('refine-cutout').disabled=!state.product;$('render-composite').disabled=!state.product;$('auto-light').disabled=!(state.product&&state.background);const transparent=state.product&&state.product.id!==state.original?.id;$('download-cutout').classList.toggle('hidden',!transparent);if(transparent)$('download-cutout').href=state.product.url;
  try {
    if(previewResult&&state.result){const im=await loadImage(state.result);if(token!==renderVersion)return;ctx.drawImage(im,0,0,w,h);return;}
    if(state.background){const im=await loadImage(state.background);if(token!==renderVersion)return;drawCover(ctx,im,w,h);}
    if(!state.product)return;
    const im=await loadImage(state.product);if(token!==renderVersion)return;
    const pw=w*p.scale,ph=pw*im.height/im.width,x=w*p.x,y=h*p.y;
    const s=p.shadow;
    if(s.opacity>0){ctx.save();ctx.filter=`blur(${s.blur*factor}px)`;ctx.fillStyle=`rgba(15,20,17,${s.opacity})`;ctx.beginPath();ctx.ellipse(x+s.offsetX*factor,y+ph*.32+s.offsetY*factor,Math.max(1,pw*.27*s.spread),Math.max(2,pw*.035*s.spread),0,0,Math.PI*2);ctx.fill();ctx.restore();}
    ctx.save();ctx.translate(x,y);ctx.rotate(p.rotation*Math.PI/180);ctx.filter=`brightness(${p.light.brightness}) contrast(${p.light.contrast}) saturate(${p.light.saturation})`;ctx.drawImage(im,-pw/2,-ph/2,pw,ph);ctx.restore();
    $('preview-subtitle').textContent=`${p.width} × ${p.height} 像素 · 拖动产品调整摆放`;
  } catch(error) {showToast(error.message,true);}
}
bindUpload('product-input',async asset=>{state.original=asset;state.product=asset;manualLight=false;summary('product-summary',asset);markComposeChanged();await maybeMatchLight();showToast('产品已导入，可以开始自动抠图。');});
bindUpload('background-input',async asset=>{state.background=asset;summary('background-summary',asset);$('clear-background').classList.remove('hidden');manualLight=false;markComposeChanged();await maybeMatchLight();});
$('clear-background').addEventListener('click',()=>{state.background=null;lastLightPair='';$('light-note').textContent='导入产品和背景后，将根据背景亮度估计光影参数。';summary('background-summary',null);$('clear-background').classList.add('hidden');markComposeChanged();});
$('auto-cutout').addEventListener('click',()=>task('正在自动抠图',async()=>{const result=await api('/api/cutout',{assetId:state.original.id,threshold:Number($('cutout-threshold')?.value||245)});state.product=assetOf(result);await loadImage(state.product);summary('product-summary',state.product);markComposeChanged();await maybeMatchLight();showToast(warningsOf(result)||'抠图完成，请检查玻璃、金属边缘和细小孔洞。');}));
function warningsOf(result) {
  const messages = result.warnings || result.warning;
  return Array.isArray(messages) ? messages.join(' · ') : messages;
}

async function maybeMatchLight(force = false) {
  if (!state.product || !state.background) return;
  const pair = state.product.id + '/' + state.background.id;
  if (!force && (lastLightPair === pair || manualLight)) return;
  try {
    const result = await api('/api/auto-light', {
      productId: state.product.id, backgroundId: state.background.id, ...state.params
    });
    if (!result.light || !result.shadow) throw new Error('光影参数未返回。');
    state.params.light = { ...state.params.light, ...result.light };
    state.params.shadow = { ...state.params.shadow, ...result.shadow };
    manualLight = false;
    lastLightPair = pair;
    $('light-note').textContent = result.note || '光影已匹配，可按画面效果微调。';
    refreshFields();
    markComposeChanged();
  } catch (error) {
    $('light-note').textContent = '未能自动匹配光影。' + error.message + ' 仍可手动设置。';
    if (force) throw error;
  }
}
$('auto-light').addEventListener('click', () => task('正在匹配光影', () => maybeMatchLight(true)));
const paramFields = {
 'scale':['scale',v=>`${Math.round(v*100)}%`], 'rotation':['rotation',v=>`${v}°`],
 'shadow-opacity':['shadow.opacity',v=>`${Math.round(v*100)}%`], 'shadow-blur':['shadow.blur',v=>v], 'shadow-spread':['shadow.spread',v=>`${Math.round(v*100)}%`],
 'shadow-x':['shadow.offsetX'], 'shadow-y':['shadow.offsetY'], 'brightness':['light.brightness',v=>`${Math.round(v*100)}%`], 'contrast':['light.contrast',v=>`${Math.round(v*100)}%`], 'saturation':['light.saturation',v=>`${Math.round(v*100)}%`], 'warmth':['light.warmth',v=>v]
};
function getPath(object,path) {return path.split('.').reduce((a,k)=>a[k],object);}
function setPath(object,path,value) {const keys=path.split('.'),last=keys.pop();keys.reduce((a,k)=>a[k],object)[last]=value;}
function refreshFields() {
  $('output-width').value=state.params.width;$('output-height').value=state.params.height;
  for(const [id,[path,format]] of Object.entries(paramFields)){const value=getPath(state.params,path);$(id).value=value;if(format)$(id+'-value').textContent=format(value);}
}
for(const [id,[path,format]] of Object.entries(paramFields))$(id).addEventListener('input',()=>{const el=$(id),v=Number(el.value);if(!Number.isFinite(v)||!el.checkValidity())return;setPath(state.params,path,v);if(path.startsWith('light.')||path.startsWith('shadow.'))manualLight=true;if(format)$(id+'-value').textContent=format(v);markComposeChanged();});
for(const id of ['output-width','output-height'])$(id).addEventListener('change',()=>{try{validateDimensions();markComposeChanged();}catch(e){showToast(e.message,true);}});
$('center-product').addEventListener('click',()=>{state.params.x=.5;state.params.y=.5;markComposeChanged();});
$('reset-compose').addEventListener('click',()=>{state.params=defaults();refreshFields();markComposeChanged();});
let drag=null;
$('compose-canvas').addEventListener('pointerdown',event=>{if(!state.product)return;event.preventDefault();const c=event.currentTarget,r=c.getBoundingClientRect();drag={id:event.pointerId,x:event.clientX,y:event.clientY,px:state.params.x,py:state.params.y,w:r.width,h:r.height};c.setPointerCapture(event.pointerId);c.classList.add('dragging');if(previewResult)markComposeChanged();});
$('compose-canvas').addEventListener('pointermove',event=>{if(!drag||event.pointerId!==drag.id)return;state.params.x=clamp(drag.px+(event.clientX-drag.x)/drag.w,-.5,1.5);state.params.y=clamp(drag.py+(event.clientY-drag.y)/drag.h,-.5,1.5);markComposeChanged();});
for(const ev of ['pointerup','pointercancel'])$('compose-canvas').addEventListener(ev,()=>{drag=null;$('compose-canvas').classList.remove('dragging');});
$('render-composite').addEventListener('click',()=>task('正在生成完整尺寸合成图',async()=>{validateDimensions();const response=await api('/api/composite',{productId:state.product.id,backgroundId:state.background?.id||null,...state.params});state.result=assetOf(response);await loadImage(state.result);previewResult=true;await drawCompose();const exported=await api('/api/export',{assetId:state.result.id,name:'产品合成图.png'});$('download-composite').href=exported.url;$('download-composite').classList.remove('hidden');$('preview-kind').textContent='当前显示完整生成结果 · 调整任意参数后可重新生成';showToast(warningsOf(response)||'合成图已生成，可以导出。');}));

// The brush edits native-resolution pixels; CSS controls display size only.
let brushMode='erase',brushActive=false,brushPrevious=null,brushOriginal=null,brushHistory=[];
const brushCanvas=$('refine-canvas'),brushContext=brushCanvas.getContext('2d',{willReadFrequently:true});
function resizeRefine() {
  const zoom=$('refine-zoom').value,stage=$('refine-stage');
  const f=zoom==='fit'?Math.min((stage.clientWidth-32)/brushCanvas.width,(stage.clientHeight-32)/brushCanvas.height,1):Number(zoom);
  brushCanvas.style.width=`${Math.max(1,Math.round(brushCanvas.width*f))}px`;brushCanvas.style.height=`${Math.max(1,Math.round(brushCanvas.height*f))}px`;
}
$('refine-cutout').addEventListener('click',()=>task('正在准备边缘修整',async()=>{const [im,original]=await Promise.all([loadImage(state.product),loadImage(state.original||state.product)]);if(im.width*im.height>24000000)throw new Error('图片较大，请先缩小至 2400 万像素以内再用画笔修整。');brushCanvas.width=im.width;brushCanvas.height=im.height;brushContext.clearRect(0,0,im.width,im.height);brushContext.drawImage(im,0,0);brushOriginal=original;brushHistory=[];$('brush-undo').disabled=true;$('refine-dialog').showModal();requestAnimationFrame(resizeRefine);}));
$('close-refine').addEventListener('click',()=>$('refine-dialog').close());$('refine-zoom').addEventListener('change',resizeRefine);$('refine-dialog').addEventListener('close',()=>{brushHistory.forEach(c=>{c.width=c.height=1;});brushHistory=[];brushOriginal=null;brushCanvas.width=brushCanvas.height=1;});
for(const mode of ['erase','restore'])$('brush-'+mode).addEventListener('click',()=>{brushMode=mode;for(const m of ['erase','restore'])$('brush-'+m).classList.toggle('selected',m===mode);});
$('brush-size').addEventListener('input',()=>$('brush-size-value').textContent=$('brush-size').value);
function brushPoint(event){const r=brushCanvas.getBoundingClientRect();return{x:(event.clientX-r.left)*brushCanvas.width/r.width,y:(event.clientY-r.top)*brushCanvas.height/r.height};}
function stampBrush(point) {
  const radius=Number($('brush-size').value)/2,previous=brushPrevious||point,steps=Math.max(1,Math.ceil(Math.hypot(point.x-previous.x,point.y-previous.y)/Math.max(1,radius*.35)));
  for(let i=1;i<=steps;i++){const x=previous.x+(point.x-previous.x)*i/steps,y=previous.y+(point.y-previous.y)*i/steps;brushContext.save();brushContext.beginPath();brushContext.arc(x,y,radius,0,Math.PI*2);if(brushMode==='erase'){brushContext.globalCompositeOperation='destination-out';brushContext.fill();}else{brushContext.clip();brushContext.globalCompositeOperation='source-over';brushContext.drawImage(brushOriginal,0,0,brushCanvas.width,brushCanvas.height);}brushContext.restore();}
  brushPrevious=point;
}
brushCanvas.addEventListener('pointerdown',e=>{e.preventDefault();const snapshot=document.createElement('canvas');snapshot.width=brushCanvas.width;snapshot.height=brushCanvas.height;snapshot.getContext('2d').drawImage(brushCanvas,0,0);brushHistory.push(snapshot);if(brushHistory.length>Math.max(1,Math.min(5,Math.floor(83886080/(brushCanvas.width*brushCanvas.height*4))))){const removed=brushHistory.shift();removed.width=removed.height=1;}$('brush-undo').disabled=false;brushActive=true;brushPrevious=null;brushCanvas.setPointerCapture(e.pointerId);stampBrush(brushPoint(e));});
brushCanvas.addEventListener('pointermove',e=>{if(brushActive)stampBrush(brushPoint(e));});for(const ev of ['pointerup','pointercancel'])brushCanvas.addEventListener(ev,()=>{brushActive=false;brushPrevious=null;});
$('brush-undo').addEventListener('click',()=>{const previous=brushHistory.pop();if(!previous)return;brushContext.clearRect(0,0,brushCanvas.width,brushCanvas.height);brushContext.drawImage(previous,0,0);previous.width=previous.height=1;$('brush-undo').disabled=!brushHistory.length;});
$('apply-refine').addEventListener('click',()=>task('正在保存透明产品图',async()=>{state.product=await uploadCanvas(brushCanvas,'产品_边缘修整.png');summary('product-summary',state.product);$('refine-dialog').close();markComposeChanged();showToast('已应用修整，原始实拍仍保留。');}));

bindUpload('split-input',async asset=>{state.splitSource=asset;state.splitAssets=[];state.splitPackageURL=null;$('download-split').classList.add('hidden');state.cuts=[];summary('split-summary',asset);$('auto-split').disabled=false;$('apply-cuts').disabled=false;$('split-results').replaceChildren();$('split-cuts').value='';renderSplitSource();$('split-status').textContent=`原图 ${asset.width} × ${asset.height} 像素`;});
$('split-threshold').addEventListener('input',()=>$('split-threshold-value').textContent=`${Math.round(Number($('split-threshold').value)*100)}%`);
function readCuts(){const text=$('split-cuts').value.trim();if(!text)return[];const cuts=text.split(/[,，\s;；]+/).filter(Boolean).map(Number);if(cuts.some(v=>!Number.isInteger(v)||v<=0||v>=state.splitSource.height))throw new Error('切点应为原图高度以内的正整数，多个切点用逗号分开。');return [...new Set(cuts)].sort((a,b)=>a-b);}
function renderSplitSource() {
  const wrap=$('split-source-wrap');wrap.replaceChildren();if(!state.splitSource)return;
  const source=document.createElement('div');source.className='split-source';const im=document.createElement('img');im.src=state.splitSource.url;im.alt='长图原片，点击添加或移除切点';source.append(im);
  for(const y of state.cuts){const line=document.createElement('div');line.className='split-line';line.style.top=`${y/state.splitSource.height*100}%`;const label=document.createElement('span');label.textContent=`${y} px`;line.append(label);source.append(line);}
  source.addEventListener('click',e=>{const r=im.getBoundingClientRect(),y=Math.round((e.clientY-r.top)/r.height*state.splitSource.height),margin=Math.max(10,8/r.height*state.splitSource.height);if(y<=0||y>=state.splitSource.height)return;const near=state.cuts.findIndex(v=>Math.abs(v-y)<margin);if(near>=0)state.cuts.splice(near,1);else state.cuts.push(y);state.cuts.sort((a,b)=>a-b);$('split-cuts').value=state.cuts.join(', ');const scroll=wrap.scrollTop;renderSplitSource();wrap.scrollTop=scroll;});
  wrap.append(source);
}
async function splitImage(manual=false) {
  const min=Number($('split-min').value),max=Number($('split-max').value);
  if(!Number.isInteger(min)||!Number.isInteger(max)||min<80||max<min)throw new Error('最大段高应不小于最小段高，最小段高至少为 80 像素。');
  const payload={assetId:state.splitSource.id,minHeight:min,maxHeight:max,threshold:Number($('split-threshold').value)};if(manual)payload.cuts=readCuts();
  const result=await api('/api/split',payload);state.splitAssets=result.assets||[];state.splitPackageURL=result.zipUrl||null;$('download-split').classList.toggle('hidden',!state.splitPackageURL);if(state.splitPackageURL)$('download-split').href=state.splitPackageURL;state.cuts=result.cuts||[];$('split-cuts').value=state.cuts.join(', ');renderSplitSource();renderSplitResults();$('split-status').textContent=`已拆成 ${state.splitAssets.length} 张 · 请检查是否切到图片或文字`;showToast('拆分完成，可调整切点后重新拆分。');
}
$('auto-split').addEventListener('click',()=>task('正在寻找长图分界',()=>splitImage(false)));$('apply-cuts').addEventListener('click',()=>task('正在按切点拆分',()=>splitImage(true)));
function renderSplitResults(){const grid=$('split-results');grid.replaceChildren();state.splitAssets.forEach((asset,i)=>{const card=document.createElement('article');card.className='result-card';const im=document.createElement('img');im.src=asset.url;im.alt=`第 ${i+1} 段`;const footer=document.createElement('footer'),title=document.createElement('span'),link=document.createElement('a');title.textContent=`${i+1} · ${asset.width}×${asset.height}`;link.textContent='导出';link.href=asset.url;link.download=`长图_第${String(i+1).padStart(2,'0')}段.png`;footer.append(title,link);card.append(im,footer);grid.append(card);});}

bindUpload('layers-input',async asset=>{state.layersSource=asset;state.layers=[];state.layersPackage=null;summary('layers-summary',asset);$('auto-layers').disabled=false;$('export-layer-preview').disabled=true;$('download-layers').classList.add('hidden');$('layer-list').replaceChildren();$('layers-warnings').classList.add('hidden');$('layers-empty').classList.add('hidden');const im=await loadImage(asset),c=$('layers-canvas'),f=Math.min(850/im.width,850/im.height,1);c.width=Math.round(im.width*f);c.height=Math.round(im.height*f);c.getContext('2d').drawImage(im,0,0,c.width,c.height);fitCanvas(c,c.width,c.height);});
$('auto-layers').addEventListener('click',()=>task('正在分离产品、文字和背景',async()=>{const result=await api('/api/layers',{assetId:state.layersSource.id,repair:$('repair-background').checked});state.layers=(result.layers||[]).map((l,i)=>({...l,key:l.id||`layer-${i}`,visible:true,originalText:l.text??'',editedText:l.text??''}));state.layersPackage=result;await Promise.all(state.layers.filter(l=>l.url).map(loadImage));renderLayerList();await drawLayers();$('export-layer-preview').disabled=!state.layers.length;$('download-layers').classList.toggle('hidden',!state.layers.length);$('download-layers').href=result.zipUrl||'#';$('layers-status').textContent=`已分出 ${state.layers.length} 个图层 · 修改文字后可导出当前画面或图层包`;const warnings=result.warnings||[];$('layers-warnings').textContent=Array.isArray(warnings)?warnings.join('；'):String(warnings);$('layers-warnings').classList.toggle('hidden',!warnings.length);showToast('分层完成，请核对文字与产品边缘。');}));
function layerBounds(layer){const b=layer.bounds||{};if(Array.isArray(b)){if(Array.isArray(b[0])){const xs=b.map(p=>p[0]),ys=b.map(p=>p[1]);return{x:Math.min(...xs),y:Math.min(...ys),width:Math.max(...xs)-Math.min(...xs),height:Math.max(...ys)-Math.min(...ys)};}return{x:b[0],y:b[1],width:b[2],height:b[3]};}return{x:b.x||0,y:b.y||0,width:b.width||b.w||state.layersSource.width,height:b.height||b.h||30};}
function renderLayerList(){const list=$('layer-list');list.replaceChildren();state.layers.forEach((layer,i)=>{const row=document.createElement('div');row.className='layer-row';const head=document.createElement('label');head.className='layer-header';const check=document.createElement('input');check.type='checkbox';check.checked=layer.visible;check.addEventListener('change',()=>{layer.visible=check.checked;drawLayers();});const thumb=document.createElement('img');thumb.src=layer.url;thumb.alt='';const name=document.createElement('span');name.className='layer-name';name.textContent=layer.name||`图层 ${i+1}`;const type=document.createElement('span');type.className='layer-type';type.textContent=({text:'文字',product:'产品',background:'背景',foreground:'产品'})[layer.type]||layer.type||'';head.append(check,thumb,name,type);row.append(head);if(layer.type==='text'&&layer.text!==undefined){const field=document.createElement('textarea');field.rows=2;field.value=layer.editedText;field.setAttribute('aria-label',`修改 ${layer.name||'识别文字'}`);field.addEventListener('input',()=>{layer.editedText=field.value;drawLayers();});const restore=document.createElement('button');restore.className='text-button';restore.textContent='恢复原始文字图层';restore.addEventListener('click',()=>{layer.editedText=layer.originalText;field.value=layer.editedText;drawLayers();});row.append(field,restore);}list.append(row);});}
function drawEditedText(ctx,layer){const b=layerBounds(layer),lines=String(layer.editedText).split('\n'),fontSize=Math.max(8,Number(layer.fontSize)||b.height/Math.max(1,lines.length)*.85);ctx.save();ctx.fillStyle=layer.color||'#222222';ctx.textBaseline='top';ctx.font=`${fontSize}px "Microsoft YaHei", sans-serif`;const widest=Math.max(...lines.map(line=>ctx.measureText(line).width),1),s=Math.min(1,b.width/widest);ctx.translate(b.x,b.y);ctx.scale(s,1);lines.forEach((line,i)=>ctx.fillText(line,0,i*fontSize*1.15));ctx.restore();}
async function renderLayerCanvas(full=false,only=null){const source=state.layersSource,w=source.width,h=source.height,c=document.createElement('canvas');const f=full?1:Math.min(850/w,850/h,1);c.width=Math.round(w*f);c.height=Math.round(h*f);const ctx=c.getContext('2d');ctx.scale(f,f);for(const layer of only?[only]:state.layers){if(!only&&!layer.visible)continue;if(layer.type==='text'&&layer.editedText!==layer.originalText)drawEditedText(ctx,layer);else if(layer.url){const im=await loadImage(layer);ctx.drawImage(im,0,0,w,h);}}return c;}
let layersRenderVersion=0;
async function drawLayers(){if(!state.layersSource||!state.layers.length)return;const token=++layersRenderVersion;try{const rendered=await renderLayerCanvas();if(token!==layersRenderVersion)return;const c=$('layers-canvas');c.width=rendered.width;c.height=rendered.height;c.getContext('2d').drawImage(rendered,0,0);fitCanvas(c,c.width,c.height);$('layers-empty').classList.add('hidden');}catch(e){showToast(e.message,true);}}
$('export-layer-preview').addEventListener('click',()=>task('正在导出分层编辑结果',async()=>{const c=await renderLayerCanvas(true),asset=await uploadCanvas(c,'分层编辑_当前画面.png'),result=await api('/api/export',{assetId:asset.id,name:'分层编辑_当前画面.png'});downloadURL(result.url,'分层编辑_当前画面.png');showToast('已导出当前可见图层的合成图。');}));
function downloadURL(url,name){const a=document.createElement('a');a.href=url;a.download=name||'';document.body.append(a);a.click();a.remove();}

// A stored ZIP requires no external library and preserves separate PNG layers and text edits.
const crcTable=(()=>{const t=new Uint32Array(256);for(let n=0;n<256;n++){let c=n;for(let k=0;k<8;k++)c=c&1?0xedb88320^(c>>>1):c>>>1;t[n]=c>>>0;}return t;})();
function crc32(bytes){let c=0xffffffff;for(const b of bytes)c=crcTable[(c^b)&255]^(c>>>8);return(c^0xffffffff)>>>0;}
function zipBlob(files){const encoder=new TextEncoder(),parts=[],central=[];let offset=0;for(const file of files){const name=encoder.encode(file.name),bytes=file.bytes,crc=crc32(bytes),header=new Uint8Array(30+name.length),v=new DataView(header.buffer);v.setUint32(0,0x04034b50,true);v.setUint16(4,20,true);v.setUint16(6,0x800,true);v.setUint32(14,crc,true);v.setUint32(18,bytes.length,true);v.setUint32(22,bytes.length,true);v.setUint16(26,name.length,true);header.set(name,30);parts.push(header,bytes);const ch=new Uint8Array(46+name.length),cv=new DataView(ch.buffer);cv.setUint32(0,0x02014b50,true);cv.setUint16(4,20,true);cv.setUint16(6,20,true);cv.setUint16(8,0x800,true);cv.setUint32(16,crc,true);cv.setUint32(20,bytes.length,true);cv.setUint32(24,bytes.length,true);cv.setUint16(28,name.length,true);cv.setUint32(42,offset,true);ch.set(name,46);central.push(ch);offset+=header.length+bytes.length;}const centralSize=central.reduce((n,a)=>n+a.length,0),end=new Uint8Array(22),ev=new DataView(end.buffer);ev.setUint32(0,0x06054b50,true);ev.setUint16(8,files.length,true);ev.setUint16(10,files.length,true);ev.setUint32(12,centralSize,true);ev.setUint32(16,offset,true);return new Blob([...parts,...central,end],{type:'application/zip'});}
function canvasBlob(canvas){return new Promise((resolve,reject)=>canvas.toBlob(blob=>blob?resolve(blob):reject(new Error('图片编码失败。')),'image/png'));}
$('download-layers').addEventListener('click',event=>{event.preventDefault();task('正在打包可编辑图层',async()=>{const files=[],manifest={name:'离线产品图工作台分层项目',width:state.layersSource.width,height:state.layersSource.height,layers:[],warning:'背景修补为推测内容；文字识别需人工核验。'};for(let i=0;i<state.layers.length;i++){const layer=state.layers[i],filename=`${String(i+1).padStart(2,'0')}_${String(layer.name||'图层').replace(/[\\/:*?"<>|]/g,'_').slice(0,60)}.png`;let bytes;if(layer.type==='text'&&layer.editedText!==layer.originalText){const c=await renderLayerCanvas(true,layer);bytes=new Uint8Array(await(await canvasBlob(c)).arrayBuffer());}else{const response=await fetch(layer.url);if(!response.ok)throw new Error('有图层无法读取，请重新拆分。');bytes=new Uint8Array(await response.arrayBuffer());}files.push({name:filename,bytes});manifest.layers.push({name:layer.name,type:layer.type,file:filename,visible:layer.visible,bounds:layer.bounds,text:layer.editedText,originalText:layer.originalText});}files.push({name:'图层项目.json',bytes:new TextEncoder().encode(JSON.stringify(manifest,null,2))});const url=URL.createObjectURL(zipBlob(files));downloadURL(url,'图片分层_可编辑图层.zip');setTimeout(()=>URL.revokeObjectURL(url),60000);showToast('图层包已导出，包含独立图片、文字和显示状态。');});});

function projectSnapshot(){return {version:1,original:state.original,product:state.product,background:state.background,result:state.result,params:state.params,splitSource:state.splitSource,splitAssets:state.splitAssets,splitPackageURL:state.splitPackageURL,manualLight,lastLightPair,cuts:state.cuts,layersSource:state.layersSource,layers:state.layers,layersPackage:state.layersPackage,tab:state.tab};}
$('save-project').addEventListener('click',()=>{if(!state.product&&!state.splitSource&&!state.layersSource)return showToast('请先导入图片再保存项目。');$('project-name').value=state.projectName||`产品图项目 ${new Date().toLocaleDateString('zh-CN')}`;$('save-dialog').showModal();$('project-name').focus();$('project-name').select();});
$('cancel-save').addEventListener('click',()=>$('save-dialog').close());
$('save-form').addEventListener('submit',event=>{event.preventDefault();const name=$('project-name').value.trim();if(!name)return;task('正在保存本地项目',async()=>{const result=await api('/api/save',{name,id:state.projectId&&name===state.projectName?state.projectId:undefined,state:projectSnapshot()});state.projectId=result.project?.id||result.id;state.projectName=name;$('save-dialog').close();showToast('项目已保存到本机。');if(state.tab==='projects')refreshProjects();});});
async function refreshProjects(){try{const response=await api('/api/projects'),list=$('project-list');list.replaceChildren();const projects=response.projects||[];if(!projects.length){const empty=document.createElement('p');empty.className='empty-inline';empty.textContent='还没有保存的项目。导入图片并完成调整后，点击右上角“保存项目”。';list.append(empty);return;}projects.forEach(project=>{const card=document.createElement('article');card.className='project-card';const thumb=document.createElement('div');thumb.className='project-thumb';const thumbURL=project.thumbnail||project.preview||project.state?.result?.url||project.state?.product?.url;if(thumbURL){const im=document.createElement('img');im.src=typeof thumbURL==='string'?thumbURL:thumbURL.url;im.alt='';thumb.append(im);}else thumb.textContent='▧';const info=document.createElement('div');info.className='project-info';const title=document.createElement('h3');title.textContent=project.name||'未命名项目';const date=document.createElement('p');const dateValue=project.updated||project.updatedAt||project.updated_at||project.createdAt||project.created_at;date.textContent=dateValue?`保存于 ${new Date(dateValue).toLocaleString('zh-CN')}`:'保存在本机';const button=document.createElement('button');button.className='button';button.textContent='继续编辑';button.addEventListener('click',()=>task('正在打开项目',()=>openProject(project.id)));info.append(title,date,button);card.append(thumb,info);list.append(card);});}catch(e){showToast(e.message,true);}}
$('refresh-projects').addEventListener('click',refreshProjects);
async function openProject(id){const response=await api('/api/project?id='+encodeURIComponent(id)),project=response.project||response,s=project.state||{};state.original=s.original||null;state.product=s.product||null;state.background=s.background||null;state.result=s.result||null;state.params={...defaults(),...(s.params||{}),shadow:{...defaults().shadow,...s.params?.shadow},light:{...defaults().light,...s.params?.light}};state.splitSource=s.splitSource||null;state.splitAssets=s.splitAssets||[];state.splitPackageURL=s.splitPackageURL||null;$('download-split').classList.toggle('hidden',!state.splitPackageURL);if(state.splitPackageURL)$('download-split').href=state.splitPackageURL;manualLight=s.manualLight??true;lastLightPair=s.lastLightPair||'';state.cuts=s.cuts||[];state.layersSource=s.layersSource||null;state.layers=(s.layers||[]).map(l=>({...l,visible:l.visible!==false,originalText:l.originalText??l.text??'',editedText:l.editedText??l.text??''}));state.layersPackage=s.layersPackage||null;state.projectName=project.name||'';state.projectId=project.id;previewResult=!!state.result;summary('product-summary',state.product);summary('background-summary',state.background);summary('split-summary',state.splitSource);summary('layers-summary',state.layersSource);$('clear-background').classList.toggle('hidden',!state.background);$('auto-split').disabled=!state.splitSource;$('apply-cuts').disabled=!state.splitSource;$('auto-layers').disabled=!state.layersSource;$('export-layer-preview').disabled=!state.layers.length;$('download-layers').classList.toggle('hidden',!state.layers.length);$('split-cuts').value=state.cuts.join(', ');$('download-composite').classList.toggle('hidden',!state.result);if(state.result)$('download-composite').href=state.result.url;refreshFields();renderSplitSource();renderSplitResults();renderLayerList();switchTab(['compose','split','layers'].includes(s.tab)?s.tab:'compose');showToast('项目已恢复，可以继续编辑。');}
$('shutdown-app').addEventListener('click',async()=>{if(!window.confirm('退出本机服务前，请确认当前项目已经保存。现在退出？'))return;try{await api('/api/shutdown',{});$('service-state').textContent='本机服务已退出';$('service-state').classList.add('error');$('engine-summary').textContent='服务已退出。下次使用请再次打开快捷启动入口。';showToast('本机服务已退出，可以关闭此页面。');}catch(e){showToast('服务可能已退出，可以关闭页面。');}});
async function init(){refreshFields();try{const status=await api('/api/status');$('service-state').textContent='本机离线运行';const parts=[status.engine?.cutout?'智能抠图可用':'白底抠图可用',status.engine?.ocr?'文字识别可用':'文字识别待配置'];$('engine-summary').textContent=parts.join(' · ');if(status.warning){$('engine-notice').textContent=Array.isArray(status.warning)?status.warning.join('；'):status.warning;$('engine-notice').classList.remove('hidden');}if(!status.engine?.cutout){const label=document.createElement('label');label.className='range-label';label.textContent='白底识别阈值';const out=document.createElement('output');out.textContent='245';const input=document.createElement('input');input.type='range';input.id='cutout-threshold';input.min='200';input.max='254';input.value='245';input.addEventListener('input',()=>out.textContent=input.value);label.append(out,input);$('auto-cutout').parentElement.after(label);}}catch(error){$('service-state').textContent='未连接本机服务';$('service-state').classList.add('error');$('engine-summary').textContent='请使用程序的快捷启动入口打开工作台。';showToast(error.message,true);}await drawCompose();}
new ResizeObserver(()=>{for(const id of ['compose-canvas','layers-canvas']){const c=$(id);if(c.offsetParent)fitCanvas(c,c.width,c.height);}}).observe($('compose-stage'));
window.addEventListener('resize',()=>{if($('refine-dialog').open)resizeRefine();});
init();
