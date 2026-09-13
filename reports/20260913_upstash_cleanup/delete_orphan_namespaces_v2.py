"""
delete_orphan_namespaces_v2.py — same job as delete_orphan_namespaces.py (remove leaked
`sf_*` staging namespaces from the `knowledge-embeddings` Upstash Vector index and their
sf:* keys from the `knowledge-system` Upstash Redis) but with ONE full Redis SCAN pass
instead of one SCAN-per-generation, which was taking ~2 min per namespace over ~6M keys.

Guards: refuses while a rebuild run is in progress; skips namespaces younger than 20 min;
never touches the serving generation or anything in sf:generation_history.
Dry-run by default; --execute deletes.

Inputs : /Users/arjundivecha/Dropbox/AAA Backup/A Working/Memory/knowledge-system/.env (first key wins)
Outputs: /Users/arjundivecha/Dropbox/AAA Backup/A Working/Memory/knowledge-system/reports/20260913_upstash_cleanup/orphan_deletion_v2_<UTC>.json
"""
import json, sys, subprocess, urllib.request, time
from datetime import datetime, timezone
from pathlib import Path
HERE=Path(__file__).resolve().parent; ROOT=HERE.parents[1]; MIN_AGE_MIN=20
env={}
for line in open(ROOT/'.env'):
    line=line.strip()
    if not line or line.startswith('#') or '=' not in line: continue
    k,v=line.split('=',1); env.setdefault(k, v.split(' #')[0].strip().strip('"'))
RU,RT,VU,VT=env['UPSTASH_REDIS_REST_URL'],env['UPSTASH_REDIS_REST_TOKEN'],env['UPSTASH_VECTOR_REST_URL'],env['UPSTASH_VECTOR_REST_TOKEN']
assert 'measured-raven' in RU and 'adjusted-iguana' in VU
def http(url,data=None,method=None,token=RT):
    req=urllib.request.Request(url,data=json.dumps(data).encode() if data is not None else None,method=method,headers={'Authorization':f'Bearer {token}','Content-Type':'application/json'})
    return json.load(urllib.request.urlopen(req,timeout=120))
def rcmd(*a): return http(RU,list(a))['result']
def rpipe(cmds): return [x.get('result') for x in http(RU+'/pipeline',cmds)]
execute='--execute' in sys.argv; now=datetime.now(timezone.utc); t0=time.time()
runs=json.loads(subprocess.run(['gh','run','list','--workflow=source-first-rebuild.yml','--limit','3','--json','status'],cwd=ROOT,capture_output=True,text=True).stdout or '[]')
if any(r['status']!='completed' for r in runs): sys.exit('REFUSING: a rebuild run is in progress')
info=http(VU+'/info',token=VT)['result']; ns=info['namespaces']; before=info['vectorCount']
cur=rcmd('GET','sf:current_generation'); hist=rcmd('GET','sf:generation_history'); hist=json.loads(hist) if isinstance(hist,str) else (hist or [])
keep={cur,*hist}
def age_min(g):
    try: return (now-datetime.strptime(g,'sf_%Y%m%dT%H%M%SZ').replace(tzinfo=timezone.utc)).total_seconds()/60
    except ValueError: return 1e9
orphans=[g for g in ns if g.startswith('sf_') and g not in keep and age_min(g)>=MIN_AGE_MIN]
total=sum(ns[g]['vectorCount'] for g in orphans)
print(f'index {before}; serving {cur}; keep {sorted(keep)}; orphans {len(orphans)} = {total} vectors -> {(before-total)/660000:.1%} after',flush=True)
# one SCAN pass, bucket sf:<gen>:* by generation
cursor='0'; buckets={}; scanned=0; manifests=[]
while True:
    c,b=rcmd('SCAN',cursor,'MATCH','sf:*','COUNT','10000'); cursor=str(c); scanned+=len(b)
    for k in b:
        parts=k.split(':')
        if len(parts)>=3 and parts[1].startswith('sf_'): buckets.setdefault(parts[1],[]).append(k)
        elif k.startswith('sf:manifest:'): manifests.append(k[len('sf:manifest:'):])
    if cursor=='0': break
print(f'scan done: {scanned} sf:* keys in {time.time()-t0:.0f}s; generations with keys: {len(buckets)}; manifests: {len(manifests)}',flush=True)
# stale redis-only generations (keys but no namespace, not kept) are also orphans
redis_only=[g for g in set(buckets)|set(manifests) if g not in ns and g not in keep and age_min(g)>=MIN_AGE_MIN]
log={'ran_at':now.isoformat(),'execute':execute,'before_vectors':before,'keep':sorted(keep),'orphans':{g:ns[g]['vectorCount'] for g in orphans},'redis_only_generations':sorted(redis_only),'redis_keys_deleted':0,'errors':[]}
print('redis-only orphan generations:',len(redis_only),flush=True)
if execute:
    for g in sorted(set(orphans)|set(redis_only)):
        keys=buckets.get(g,[])+[f'sf:manifest:{g}']
        for i in range(0,len(keys),500): rpipe([['UNLINK',*keys[i:i+500]]])
        log['redis_keys_deleted']+=len(keys)
        if g in ns:
            try: http(f'{VU}/delete-namespace/{g}',method='DELETE',token=VT)
            except Exception as e: log['errors'].append(f'{g}: {e}')
        print(f'deleted {g}: {ns.get(g,{}).get("vectorCount",0)} vectors, {len(keys)} redis keys ({time.time()-t0:.0f}s)',flush=True)
    after=http(VU+'/info',token=VT)['result']; log['after_vectors']=after['vectorCount']; log['after_namespaces']=sorted(after['namespaces'])
    print(f"AFTER: {after['vectorCount']} vectors = {after['vectorCount']/660000:.1%}; namespaces {sorted(after['namespaces'])}",flush=True)
out=HERE/f"orphan_deletion_v2_{now.strftime('%Y%m%dT%H%M%SZ')}{'' if execute else '_dryrun'}.json"; out.write_text(json.dumps(log,indent=1)); print('log ->',out)
