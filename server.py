"""Offline-only image workshop. Binds loopback; no remote image requests."""
from __future__ import annotations
import argparse, base64, binascii, contextlib, io, json, os, re, shutil, sys, threading, time, uuid, webbrowser, zipfile
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from PIL import Image, ImageOps
from engine import ImageEngine

APP = Path(__file__).resolve().parent
Image.MAX_IMAGE_PIXELS = 120_000_000
ID = re.compile(r'^[a-f0-9]{32}$')
MAX_BODY = 90 * 1024 * 1024

def json_write(path, value):
    with path.open('x', encoding='utf-8') as f:
        json.dump(value, f, ensure_ascii=False, indent=2)

def safe_name(value, fallback='图片'):
    return re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', str(value or fallback))[:90].strip('. ') or fallback

def offline_network_guard(event, args):
    # Model libraries may open local sockets. Never let a missing model
    # initiate a download or send an image outside the workstation.
    if event == 'socket.connect':
        address = args[1]
        if isinstance(address, tuple) and address[0] not in ('127.0.0.1', '::1', 'localhost'):
            raise PermissionError('离线模式仅允许本机连接。')
    elif event == 'socket.getaddrinfo':
        if args[0] not in (None, '', '127.0.0.1', '::1', 'localhost'):
            raise PermissionError('离线模式仅解析本机地址。')

def reuse_running_window(data):
    """Check that a saved port still serves this workspace before reopening it."""
    from urllib.request import ProxyHandler, build_opener
    try:
        port = int((data / '本机端口').read_text(encoding='ascii'))
        url = f'http://127.0.0.1:{port}'
        with build_opener(ProxyHandler({})).open(url + '/api/status', timeout=1) as reply:
            status = json.load(reply)
        if status.get('name') == '离线产品图工作台' and status.get('dataPath') == str(data):
            webbrowser.open(url)
            return True
    except (OSError, ValueError):
        pass
    return False

class Workshop:
    def __init__(self, data):
        self.data = Path(data).resolve()
        self.assets = self.data / 'assets'
        self.projects = self.data / 'projects'
        self.exports = self.data / 'exports'
        self.jobs = self.data / 'layers'
        for folder in (self.assets, self.projects, self.exports, self.jobs):
            folder.mkdir(parents=True, exist_ok=True)
        self.engine = ImageEngine(APP)
        self.lock = threading.Lock()

    def asset_path(self, aid):
        if not isinstance(aid, str) or not ID.fullmatch(aid):
            raise ValueError('图片编号无效，请重新导入图片。')
        path = self.assets / (aid + '.png')
        if not path.is_file():
            raise ValueError('这张图片不在本地素材库中，请重新导入。')
        return path

    def asset_meta(self, aid):
        path = self.asset_path(aid)
        meta = path.with_suffix('.json')
        if meta.is_file():
            return json.loads(meta.read_text(encoding='utf-8'))
        with Image.open(path) as im:
            return {'id':aid, 'url':'/assets/'+path.name, 'name':path.name, 'width':im.width, 'height':im.height}

    def register(self, path, name):
        path = Path(path)
        if path.parent != self.assets or not ID.fullmatch(path.stem):
            dest = self.assets / (uuid.uuid4().hex + '.png')
            with Image.open(path) as im:
                im.convert('RGBA').save(dest)
            path = dest
        with Image.open(path) as im:
            meta = {'id':path.stem, 'url':'/assets/'+path.name, 'width':im.width, 'height':im.height, 'name':safe_name(name), 'created':datetime.now().isoformat(timespec='seconds')}
        json_write(path.with_suffix('.json'), meta)
        return meta

    def new_output(self):
        return self.assets / (uuid.uuid4().hex + '.png')

    def upload(self, body):
        encoded = body.get('data', '')
        if not isinstance(encoded, str) or not encoded.startswith('data:image/') or ';base64,' not in encoded:
            raise ValueError('请选择 PNG、JPEG 或 WebP 图片。')
        try:
            raw = base64.b64decode(encoded.split(',',1)[1], validate=True)
        except (binascii.Error, ValueError):
            raise ValueError('图片数据不完整，请重新导入。')
        if len(raw) > 65*1024*1024:
            raise ValueError('图片文件不能超过 65 MB。')
        with Image.open(io.BytesIO(raw)) as image:
            if image.width * image.height > 120_000_000 or image.width > 40000 or image.height > 80000:
                raise ValueError('图片过大，请先分段到 1.2 亿像素以内。')
            image = ImageOps.exif_transpose(image).convert('RGBA')
            dest = self.new_output()
            image.save(dest)
        return {'asset':self.register(dest, body.get('name','导入图片'))}

    def action(self, route, body):
        if route == '/api/upload':
            return self.upload(body)
        if route == '/api/save':
            state = body.get('state', {})
            if len(json.dumps(state, ensure_ascii=False)) > 4_000_000:
                raise ValueError('项目配置过大。请保存图片到素材库后再保存项目。')
            project = {'id':uuid.uuid4().hex,'name':safe_name(body.get('name'),'未命名项目'),'updated':datetime.now().isoformat(timespec='seconds'),'state':state,'version':1}
            json_write(self.projects/(project['id']+'.json'),project)
            return {'project':project}
        if route == '/api/export':
            src = self.asset_path(body.get('assetId'))
            name = safe_name(body.get('name'),'合成产品图')
            if name.lower().endswith('.png'): name=name[:-4]
            dest = self.exports/(name+'_'+datetime.now().strftime('%Y%m%d_%H%M%S')+'_'+uuid.uuid4().hex[:6]+'.png')
            shutil.copyfile(src,dest)
            return {'url':'/exports/'+dest.name,'name':dest.name,'path':str(dest)}
        if not self.lock.acquire(blocking=False):
            raise RuntimeError('正在处理上一张图片，请稍后再试。')
        try:
            if route == '/api/cutout':
                src=self.asset_path(body.get('assetId')); dest=self.new_output()
                warnings=self.engine.cutout(src,dest,threshold=body.get('threshold',245))
                return {'asset':self.register(dest,'抠图_'+self.asset_meta(src.stem)['name']),'warnings':warnings or []}
            if route == '/api/auto-light':
                product = self.asset_path(body.get('productId'))
                background = self.asset_path(body.get('backgroundId'))
                return self.engine.auto_settings(product,background,body)
            if route == '/api/composite':
                src=self.asset_path(body.get('productId')); bg=self.asset_path(body['backgroundId']) if body.get('backgroundId') else None; dest=self.new_output()
                warnings=self.engine.composite(src,bg,dest,body)
                return {'asset':self.register(dest,'合成产品图'),'warnings':warnings or []}
            if route == '/api/split':
                src=self.asset_path(body.get('assetId')); job=self.jobs/uuid.uuid4().hex; job.mkdir()
                result=self.engine.split(src,job,min_height=int(body.get('minHeight',250)),max_height=int(body.get('maxHeight',1600)),threshold=float(body.get('threshold',.97)),cuts=body.get('cuts'))
                assets=[]
                for index,part in enumerate(result['segments']):
                    meta=self.register(part['path'],f'分段_{index+1:02d}')
                    meta['box']=part['box']; assets.append(meta)
                zname='长图分段_'+uuid.uuid4().hex[:10]+'.zip'
                with zipfile.ZipFile(self.exports/zname,'x',zipfile.ZIP_DEFLATED) as z:
                    for index,asset in enumerate(assets):z.write(self.asset_path(asset['id']),f'{index+1:02d}.png')
                return {'assets':assets,'cuts':result['cuts'],'warnings':result.get('warnings',[]),'zipUrl':'/exports/'+zname}
            if route == '/api/layers':
                src=self.asset_path(body.get('assetId')); jid=uuid.uuid4().hex; job=self.jobs/jid;job.mkdir()
                result=self.engine.layers(src,job,repair=body.get('repair') is True)
                layers=[]
                for index,layer in enumerate(result['layers']):
                    asset=self.register(layer['path'],layer['name'])
                    layers.append({**{k:v for k,v in layer.items() if k!='path'},'id':asset['id'],'url':asset['url'],'width':asset['width'],'height':asset['height']})
                source = self.asset_meta(src.stem)
                info={'source':source,'layers':layers,'warnings':result.get('warnings',[]),'note':'像素文字图层保留原字形。识别文字可编辑。修补背景由邻近像素推测。'}
                json_write(job/'图层说明.json',info)
                zname='分层工程_'+jid[:10]+'.zip'
                with zipfile.ZipFile(self.exports/zname,'x',zipfile.ZIP_DEFLATED) as z:
                    z.write(src,'原图.png'); z.write(job/'图层说明.json','图层说明.json')
                    for index,layer in enumerate(layers):z.write(self.asset_path(layer['id']),f'{index:02d}_{safe_name(layer["name"])}.png')
                package = {**info,'projectId':jid,'zipUrl':'/exports/'+zname}
                editable = [{**layer,'visible':True,'key':layer['id'],'originalText':layer.get('text',''),'editedText':layer.get('text','')} for layer in layers]
                state = {'tab':'layers','layersSource':source,'layers':editable,'layersPackage':package}
                project = {'id':jid,'name':'分层_'+source['name'],'updated':datetime.now().isoformat(timespec='seconds'),'state':state,'version':1}
                json_write(self.projects/(jid+'.json'),project)
                return package
            raise ValueError('未找到这项操作。')
        finally:
            self.lock.release()

class Handler(BaseHTTPRequestHandler):
    server_version='OfflineWorkshop/1.0'
    def log_message(self, *_): pass
    @property
    def app(self):return self.server.app

    def local_request(self, post=False):
        expected={'127.0.0.1:'+str(self.server.server_port),'localhost:'+str(self.server.server_port)}
        if self.headers.get('Host','') not in expected:
            self.error('仅允许本机访问。',403);return False
        origin=self.headers.get('Origin')
        if origin and origin not in {'http://'+host for host in expected}:
            self.error('不允许其他网页访问本地图片。',403);return False
        if post and 'application/json' not in self.headers.get('Content-Type',''):
            self.error('请求格式不正确。',415);return False
        return True

    def send_json(self,value,status=200):
        data=json.dumps(value,ensure_ascii=False).encode('utf-8')
        self.send_response(status);self.send_header('Content-Type','application/json; charset=utf-8');self.send_header('Cache-Control','no-store');self.send_header('X-Content-Type-Options','nosniff');self.send_header('Content-Length',str(len(data)));self.end_headers();self.wfile.write(data)

    def error(self,msg,status=400):self.send_json({'error':msg},status)

    def file(self,path,ctype,download=False):
        if not path.is_file():self.error('文件不存在。',404);return
        self.send_response(200);self.send_header('Content-Type',ctype);self.send_header('X-Content-Type-Options','nosniff');self.send_header('Cache-Control','no-cache');self.send_header('Content-Length',str(path.stat().st_size))
        if download:
            from urllib.parse import quote
            self.send_header('Content-Disposition',"attachment; filename*=UTF-8''"+quote(path.name))
        if ctype.startswith('text/html'):
            self.send_header('Content-Security-Policy',"default-src 'self'; img-src 'self' data: blob:; style-src 'self' 'unsafe-inline'; script-src 'self'; connect-src 'self'; font-src 'self'; object-src 'none'; frame-ancestors 'none'; base-uri 'self'")
        self.end_headers()
        with path.open('rb') as f:shutil.copyfileobj(f,self.wfile)

    def do_GET(self):
        if not self.local_request():return
        u=urlsplit(self.path);route=u.path
        try:
            if route=='/api/status':
                engine=self.app.engine.status()
                self.send_json({**engine,'dataPath':str(self.app.data)});return
            if route=='/api/projects':
                items=[]
                for p in self.app.projects.glob('*.json'):
                    with contextlib.suppress(ValueError,OSError):
                        x=json.loads(p.read_text(encoding='utf-8'))
                        item={k:x.get(k) for k in ('id','name','updated','version')}
                        item['updatedAt']=x.get('updated'); state=x.get('state') or {}
                        for key in ('result','product','layersSource','splitSource'):
                            if isinstance(state.get(key),dict) and state[key].get('id'):
                                item['thumbnail']='/assets/'+str(state[key]['id'])+'.png'; break
                        items.append(item)
                self.send_json({'projects':sorted(items,key=lambda x:x.get('updated') or '',reverse=True)});return
            if route=='/api/project':
                pid=parse_qs(u.query).get('id',[''])[0]
                if not ID.fullmatch(pid):raise ValueError('项目编号不正确。')
                p=self.app.projects/(pid+'.json')
                if not p.is_file():raise ValueError('找不到这个项目。')
                self.send_json({'project':json.loads(p.read_text(encoding='utf-8'))});return
            if route.startswith('/assets/'):
                aid=route.removeprefix('/assets/').removesuffix('.png');self.file(self.app.asset_path(aid),'image/png');return
            if route.startswith('/exports/'):
                from urllib.parse import unquote
                name=unquote(route.removeprefix('/exports/'))
                if name!=Path(name).name or '/' in name or '\\' in name or '..' in name:raise ValueError('导出文件名无效。')
                self.file(self.app.exports/name,'application/zip' if name.endswith('.zip') else 'image/png',True);return
            known={'/':'index.html','/index.html':'index.html','/app.js':'app.js','/style.css':'style.css','/static/app.js':'app.js','/static/style.css':'style.css'}
            if route in known:
                name=known[route];ctype={'html':'text/html; charset=utf-8','js':'text/javascript; charset=utf-8','css':'text/css; charset=utf-8'}[name.rsplit('.',1)[1]]
                self.file(APP/'static'/name,ctype);return
            self.error('页面不存在。',404)
        except (ValueError,OSError) as e:self.error(str(e))

    def do_POST(self):
        if not self.local_request(post=True):return
        try:
            length=int(self.headers.get('Content-Length','0'))
            if length<=0 or length>MAX_BODY:raise ValueError('请求内容为空或文件过大。')
            body=json.loads(self.rfile.read(length).decode('utf-8'))
            if not isinstance(body,dict):raise ValueError('请求格式错误。')
            route=urlsplit(self.path).path
            if route=='/api/shutdown':
                self.send_json({'ok':True});threading.Thread(target=self.server.shutdown,daemon=True).start();return
            self.send_json(self.app.action(route,body))
        except (ValueError, OSError, RuntimeError, KeyError) as e:self.error(str(e))
        except Exception as e:self.error('处理未完成：'+str(e)[:250],500)

def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--port',type=int,default=0)
    parser.add_argument('--data',default=str(APP/'data'))
    parser.add_argument('--no-browser',action='store_true')
    args=parser.parse_args()
    sys.addaudithook(offline_network_guard)
    app=Workshop(args.data)
    if not args.no_browser and reuse_running_window(app.data):
        return
    server=ThreadingHTTPServer(('127.0.0.1',args.port),Handler);server.app=app
    url='http://127.0.0.1:'+str(server.server_port)
    if sys.stdout:print(url,flush=True)
    if not args.no_browser:
        (app.data / '本机端口').write_text(str(server.server_port), encoding='ascii')
        webbrowser.open(url)
    try:server.serve_forever(poll_interval=.3)
    except KeyboardInterrupt:pass
    finally:server.server_close()

if __name__=='__main__':main()
