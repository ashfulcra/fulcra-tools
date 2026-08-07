import time
from coord_engine import cli, projection as projection_mod
from coord_engine.transport import FulcraFileTransport

ops = []
class Spy(FulcraFileTransport):
    def list_dir(self, p):
        t=time.monotonic(); r=super().list_dir(p); ops.append(("list",p,time.monotonic()-t)); return r
    def read(self, p):
        t=time.monotonic(); r=super().read(p); ops.append(("read",p,time.monotonic()-t)); return r

t = Spy(); now = cli._iso(cli._now())
doc_sink=[]; feed_sink=[]
rows,_,_ = cli._load_rows_status(t,"fulcra",doc_sink=doc_sink,feed_sink=feed_sink,
                                 feed_section_key=projection_mod.NEEDS_ME_KEY)
ops.clear()
t0=time.monotonic()
held, unresolved = cli._held_roles_for_rows(t,"fulcra","coord-maintainer",rows,now=now)
el=time.monotonic()-t0
print(f"elapsed {el:.1f}s over {len(ops)} transport ops")
print(f"held={held} unresolved={unresolved}")
from collections import Counter
c=Counter(k for k,_,_ in ops); print("op kinds:",dict(c))
print("slowest:")
for k,p,d in sorted(ops,key=lambda x:-x[2])[:8]: print(f"   {d:5.2f}s {k} {p}")
print("lease-shard reads:", sum(1 for k,p,_ in ops if "/leases/" in p))
