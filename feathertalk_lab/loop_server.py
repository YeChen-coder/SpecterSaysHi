"""Separate moving-base demonstration service; no DH connection or integration."""
import base64,hashlib,json,struct,threading,time,socket
from pathlib import Path
from http.server import SimpleHTTPRequestHandler,ThreadingHTTPServer
import cv2,numpy as np
from loop_core import LoopEngine,LoopSession
from stream_core import MODES

engine=LoopEngine();lock=threading.Lock()

def exact(stream,n):
    data=bytearray()
    while len(data)<n:
        part=stream.read(n-len(data))
        if not part:raise EOFError
        data.extend(part)
    return bytes(data)

class Handler(SimpleHTTPRequestHandler):
    protocol_version='HTTP/1.1'
    def setup(self):
        super().setup()
        self.connection.setsockopt(socket.IPPROTO_TCP,socket.TCP_NODELAY,1)
    def __init__(self,*args,**kwargs):super().__init__(*args,directory='/work',**kwargs)
    def do_GET(self):
        path=self.path.split('?')[0]
        if path in ('/avatar/video','/avatar/poster'):
            asset=engine.idle_video_path if path=='/avatar/video' else engine.idle_poster_path
            self.path=str(asset).removeprefix('/work')
            return super().do_GET()
        if self.path.split('?')[0] in ('/','/health'):
            if self.path.split('?')[0]=='/health':
                data=json.dumps({'ready':True,'checkpoint':'retrain_20260929/best.pth','base':engine.manifest,'modes':MODES,'packet_ms':100,'load_seconds':engine.load_seconds,'stream_shape_warmup_seconds':engine.stream_shape_warmup_seconds,'teeth_modes':['model','new'] if engine.teeth is not None else ['model']}).encode();mime='application/json'
            else:data=Path('/lab/loop.html').read_bytes();mime='text/html; charset=utf-8'
            self.send_response(200);self.send_header('Content-Type',mime);self.send_header('Content-Length',str(len(data)));self.end_headers();self.wfile.write(data);return
        if self.path!='/ws':return super().do_GET()
        if self.headers.get('Upgrade','').lower()!='websocket':self.send_error(400);return
        key=self.headers.get('Sec-WebSocket-Key','');digest=base64.b64encode(hashlib.sha1((key+'258EAFA5-E914-47DA-95CA-C5AB0DC85B11').encode()).digest()).decode()
        self.send_response(101);self.send_header('Upgrade','websocket');self.send_header('Connection','Upgrade');self.send_header('Sec-WebSocket-Accept',digest);self.end_headers()
        if not lock.acquire(blocking=False):self.send_json({'error':'已有试听正在运行，请稍后重试'});self.close_connection=True;return
        session=None;rows=[];start=None
        try:
            while True:
                header=exact(self.rfile,2);opcode=header[0]&15;masked=header[1]&128;n=header[1]&127
                if n==126:n=struct.unpack('!H',exact(self.rfile,2))[0]
                elif n==127:n=struct.unpack('!Q',exact(self.rfile,8))[0]
                if n>1024*1024:raise ValueError('message too large')
                mask=exact(self.rfile,4) if masked else None;payload=exact(self.rfile,n)
                if mask:payload=bytes(c^mask[i%4] for i,c in enumerate(payload))
                if opcode==8:break
                if opcode==9:self.send_frame(payload,10);continue
                if opcode==1:
                    config=json.loads(payload)
                    if config.get('type')=='start':
                        if session is not None:raise ValueError('session already started')
                        mode=config.get('mode','fast')
                        if mode not in MODES:raise ValueError('unknown mode')
                        session=LoopSession(engine,mode,config.get('start_frame',0),config.get('teeth','new'));start=time.perf_counter()
                        self.send_json({'type':'ready','start_frame':session.start_frame,'mode':mode,'teeth':session.teeth_mode,'fps':25,'base_frames':engine.count,'width':engine.manifest['width'],'height':engine.manifest['height']});continue
                    if config.get('type')=='end' and session is not None:
                        self.emit(session.iter_push([],True),session,start,rows)
                        result={'type':'done','frames':session.next_frame,'start_frame':session.start_frame,'mode':session.mode,'teeth':session.teeth_mode,'teeth_status':engine.teeth.status() if session.teeth_mode=='new' else None,'server_seconds':time.perf_counter()-start,'rows':rows,'blocks':session.blocks}
                        Path('/work/output/loop_base/live_last_session.json').write_text(json.dumps(result,indent=2));self.send_json(result);break
                if opcode==2:
                    if session is None:raise ValueError('start session first')
                    if len(payload)%4:raise ValueError('invalid PCM byte count')
                    if len(session.pcm)+len(payload)//4>30*16000:raise ValueError('maximum 30 seconds')
                    pcm=np.frombuffer(payload,dtype='<f4')
                    if not np.isfinite(pcm).all():raise ValueError('invalid PCM')
                    self.emit(session.iter_push(pcm),session,start,rows)
        except (EOFError,ConnectionResetError,BrokenPipeError):pass
        except Exception as error:
            print('loop session error',repr(error),flush=True)
            try:self.send_json({'error':str(error)})
            except OSError:pass
        finally:lock.release();self.close_connection=True
    def emit(self,frames,session,start,rows):
        for idx,image,pred in frames:
            begin=time.perf_counter();ok,jpeg=cv2.imencode('.jpg',image,[cv2.IMWRITE_JPEG_QUALITY,95])
            if not ok:raise RuntimeError('JPEG encoding failed')
            row={**session.rows[idx],'server_elapsed_ms':(time.perf_counter()-start)*1000,'jpeg_ms':(time.perf_counter()-begin)*1000};rows.append(row)
            self.send_json({'type':'frame',**row,'image':base64.b64encode(jpeg).decode()})
    def send_json(self,value):self.send_frame(json.dumps(value,separators=(',',':')).encode())
    def send_frame(self,payload,opcode=1):
        n=len(payload);header=bytes([128|opcode,n]) if n<126 else bytes([128|opcode,126])+struct.pack('!H',n) if n<65536 else bytes([128|opcode,127])+struct.pack('!Q',n)
        self.wfile.write(header+payload);self.wfile.flush()

def serve():
    print('FeatherTalk moving-base demo ready :8080',flush=True)
    ThreadingHTTPServer(('0.0.0.0',8080),Handler).serve_forever()

if __name__ == '__main__':
    serve()
