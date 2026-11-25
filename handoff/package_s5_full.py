#!/usr/bin/env python3
"""Stream a complete project tar.gz into bounded parts, recording per-file hashes."""
import os,stat,tarfile,gzip,hashlib,json,time
from pathlib import Path
ROOT=Path('/Users/lixiuqi/Documents/ChatGPT/相机')
OUT=ROOT/'github-private/full-handoff-20261125'
SOURCES=[(ROOT/n,n)for n in ('s5-emulator','s5-v29-offline','s5-video-unlocker','s5-sd-loader-prototype')]+[(Path('/Users/lixiuqi/相机功能验证'),'external/相机功能验证')]
OUT.mkdir(parents=True,exist_ok=True)
class Parts:
 def __init__(self):self.file=None;self.n=0;self.size=0;self.total=0;self.hash=None;self.parts=[]
 def open(self):
  self.path=OUT/('s5-full-handoff.tar.gz.part-%03d'%self.n);self.n+=1
  self.file=self.path.open('xb');self.hash=hashlib.sha256();self.size=0
 def finish(self):
  if self.file:
   self.file.close();self.parts.append({'name':self.path.name,'bytes':self.size,'sha256':self.hash.hexdigest()});self.file=None
 def write(self,data):
  count=len(data);v=memoryview(data)
  while v:
   if self.file is None:self.open()
   n=min(len(v),512*1024*1024-self.size);part=v[:n];self.file.write(part);self.hash.update(part);self.size+=n;self.total+=n;v=v[n:]
   if self.size==512*1024*1024:self.finish()
  return count
 def flush(self):
  if self.file:self.file.flush()
class Reader:
 def __init__(self,f):self.f=f;self.h=hashlib.sha256();self.bytes=0
 def read(self,n=-1):
  b=self.f.read(n);self.h.update(b);self.bytes+=len(b);return b

def entries(root):
 yield root
 if root.is_dir() and not root.is_symlink():
  for parent,dirs,files in os.walk(root,followlinks=False):
   dirs.sort();files.sort()
   for name in dirs+files:yield Path(parent)/name
p=Parts();count=0;total=0;special=[];start=time.time();last=start
manifest=OUT/'file-manifest.jsonl'
with manifest.open('x')as report, gzip.GzipFile(filename='',fileobj=p,mode='wb',compresslevel=1,mtime=0)as gz,tarfile.open(fileobj=gz,mode='w|',format=tarfile.PAX_FORMAT)as archive:
 for source,prefix in SOURCES:
  for f in entries(source):
   arc=prefix if f==source else prefix+'/'+str(f.relative_to(source))
   info=archive.gettarinfo(str(f),arcname=arc)
   if info is None:
    special.append({'path':arc,'reason':'socket or unsupported runtime node'});continue
   row={'path':arc,'size':info.size,'mode':info.mode,'mtime':info.mtime,'type':'file' if info.isfile()else 'symlink' if info.issym()else 'hardlink' if info.islnk()else 'directory','linkname':info.linkname}
   if info.isfile():
    before=f.stat()
    with f.open('rb')as raw:
     reader=Reader(raw);archive.addfile(info,reader)
    after=f.stat()
    if (before.st_size,before.st_mtime_ns)!=(after.st_size,after.st_mtime_ns):raise RuntimeError('Source changed while archiving: '+str(f))
    assert reader.bytes==info.size
    row['sha256']=reader.h.hexdigest();total+=info.size
   else:archive.addfile(info)
   report.write(json.dumps(row,ensure_ascii=False)+'\n');count+=1
   if time.time()-last>15:
    last=time.time();(OUT/'progress.json').write_text(json.dumps({'entries':count,'input_bytes':total,'compressed_bytes':p.total,'seconds':round(last-start),'last':arc},ensure_ascii=False));print('entries=%d rawGiB=%.2f compressedMiB=%.1f seconds=%d'%(count,total/2**30,p.total/2**20,last-start),flush=True)
p.finish()
summary={'created':'2026-11-25','archive_format':'concatenated parts form one tar.gz','part_size_limit':512*1024*1024,'entries':count,'regular_file_bytes':total,'compressed_bytes':p.total,'sources':[{'source':str(s),'archive_prefix':a}for s,a in SOURCES],'parts':p.parts,'excluded_special_runtime_nodes':special,'file_manifest_sha256':hashlib.sha256(manifest.read_bytes()).hexdigest(),'original_absolute_symlinks_preserved':True}
(OUT/'archive-manifest.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2)+'\n')
(OUT/'SHA256SUMS').write_text(''.join(x['sha256']+'  '+x['name']+'\n'for x in p.parts)+hashlib.sha256(manifest.read_bytes()).hexdigest()+'  file-manifest.jsonl\n')
print(json.dumps({'completed':True,'entries':count,'raw_bytes':total,'compressed_bytes':p.total,'parts':len(p.parts),'special':len(special)}),flush=True)
