"""Support browser seeking in the locally served idle video and audio."""
import re
from pathlib import Path
from http.server import SimpleHTTPRequestHandler

class RangeRequestHandler(SimpleHTTPRequestHandler):
    def send_head(self):
        self.range_remaining=None
        path=Path(self.translate_path(self.path));requested=self.headers.get('Range')
        if not requested or not path.is_file() or path.suffix not in ('.mp4','.wav'):
            return super().send_head()
        match=re.fullmatch(r'bytes=(\d*)-(\d*)',requested.strip())
        if not match:return super().send_head()
        size=path.stat().st_size;left,right=match.groups()
        start=int(left) if left else max(0,size-int(right or size))
        end=min(size-1,int(right)) if right and left else size-1
        if start>=size or end<start:
            self.send_response(416);self.send_header('Content-Range',f'bytes */{size}')
            self.send_header('Content-Length','0');self.end_headers();return None
        stream=path.open('rb');stream.seek(start);self.range_remaining=end-start+1
        self.send_response(206);self.send_header('Content-Type',self.guess_type(str(path)))
        self.send_header('Accept-Ranges','bytes');self.send_header('Content-Range',f'bytes {start}-{end}/{size}')
        self.send_header('Content-Length',str(self.range_remaining));self.end_headers();return stream

    def copyfile(self,source,outputfile):
        remaining=getattr(self,'range_remaining',None)
        if remaining is None:return super().copyfile(source,outputfile)
        while remaining:
            chunk=source.read(min(65536,remaining))
            if not chunk:break
            outputfile.write(chunk);remaining-=len(chunk)
