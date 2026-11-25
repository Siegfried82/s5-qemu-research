#!/usr/bin/env python3
"""Verify all parts and archived regular-file hashes; optionally restore to a NEW directory."""
import argparse,gzip,hashlib,io,json,os,tarfile
from pathlib import Path,PurePosixPath

def digest(path):
 h=hashlib.sha256()
 with path.open('rb')as f:
  for b in iter(lambda:f.read(8*1024*1024),b''):h.update(b)
 return h.hexdigest()
class PartsReader(io.RawIOBase):
 def __init__(self,paths):self.paths=iter(paths);self.f=None
 def readable(self):return True
 def readinto(self,b):
  while True:
   if self.f is None:
    try:self.f=next(self.paths).open('rb')
    except StopIteration:return 0
   n=self.f.readinto(b)
   if n:return n
   self.f.close();self.f=None
 def close(self):
  if self.f:self.f.close()
  super().close()
def main():
 ap=argparse.ArgumentParser(description=__doc__);ap.add_argument('--assets',type=Path,default=Path(__file__).resolve().parent);ap.add_argument('--output',type=Path,help='NEW, nonexistent directory; omit for verification only');a=ap.parse_args()
 root=a.assets.resolve();m=json.loads((root/'archive-manifest.json').read_text())
 for part in m['parts']:
  p=root/part['name'];assert p.stat().st_size==part['bytes'],p
  assert digest(p)==part['sha256'],p
  print('Part SHA256 OK:',p.name,flush=True)
 assert digest(root/'file-manifest.jsonl')==m['file_manifest_sha256']
 entries={r['path']:r for r in map(json.loads,(root/'file-manifest.jsonl').read_text().splitlines())}
 dest=a.output.resolve()if a.output else None
 if dest:dest.mkdir(parents=True,exist_ok=False)
 count=0;regular_bytes=0;seen=set()
 with io.BufferedReader(PartsReader([root/p['name']for p in m['parts']]))as raw,gzip.GzipFile(fileobj=raw)as gz,tarfile.open(fileobj=gz,mode='r|')as archive:
  for member in archive:
   name=member.name;path=PurePosixPath(name)
   assert not path.is_absolute() and '..'not in path.parts,name
   assert name in entries and name not in seen,name
   seen.add(name);r=entries[name];assert member.size==r['size'] and member.linkname==r['linkname'],name
   kind='file'if member.isfile()else 'symlink'if member.issym()else 'hardlink'if member.islnk()else 'directory'if member.isdir()else 'other'
   assert kind==r['type'],(name,kind,r['type'])
   target=dest/name if dest else None
   if target:
    assert target.parent.resolve().is_relative_to(dest),name
    target.parent.mkdir(parents=True,exist_ok=True)
   if member.isfile():
    h=hashlib.sha256();n=0;stream=archive.extractfile(member);out=target.open('xb')if target else None
    try:
     for b in iter(lambda:stream.read(8*1024*1024),b''):
      h.update(b);n+=len(b)
      if out:out.write(b)
    finally:
     if out:out.close()
    assert n==r['size'] and h.hexdigest()==r['sha256'],name
    regular_bytes+=n
    if target:os.chmod(target,member.mode);os.utime(target,(member.mtime,member.mtime))
   elif target:
    if member.isdir():target.mkdir(exist_ok=True)
    elif member.issym():target.symlink_to(member.linkname)
    elif member.islnk():
     link=PurePosixPath(member.linkname);assert not link.is_absolute()and '..'not in link.parts
     source=dest/member.linkname;assert source.resolve().is_relative_to(dest)
     os.link(source,target)
   count+=1
  # Consume gzip trailer to check CRC, including tar end padding.
  while gz.read(8*1024*1024):pass
 assert seen==set(entries)and count==m['entries']
 print(json.dumps({'verified':True,'entries':count,'regular_bytes':regular_bytes,'restored_to':str(dest)if dest else None}))
if __name__=='__main__':main()
