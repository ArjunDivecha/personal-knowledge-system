"""
delete_orphan_namespaces.py — remove leaked staging namespaces from the Upstash Vector
index `knowledge-embeddings` (adjusted-iguana-29437) and their sf:* keys from the
`knowledge-system` Redis (measured-raven-41051).

An orphan is an `sf_*` namespace that is NOT the serving generation and NOT in
`sf:generation_history` (live + 2 rollbacks). Safety guards:
  * refuses if a GitHub rebuild run is in progress
  * skips any namespace younger than MIN_AGE_MIN (could be an in-flight stage)
  * dry-run by default; pass --execute to delete

Inputs : /Users/arjundivecha/Dropbox/AAA Backup/A Working/Memory/knowledge-system/.env (first key wins)
Outputs: /Users/arjundivecha/Dropbox/AAA Backup/A Working/Memory/knowledge-system/reports/20260913_upstash_cleanup/orphan_deletion_<UTC>.json
"""
import json, sys, subprocess, urllib.request
from datetime import datetime, timezone, timedelta
from pathlib import Path
HERE=Path(__file__).resolve().parent; ROOT=HERE.parents[1]
MIN_AGE_MIN=20
env={}
for line in open(ROOT/'.env'):
    line=line.strip()
    if not line or line.startswith('#') or '=' not in line: continue
    k,v=line.split('=',1); env.setdefault(k, v.split(' #')[0].strip().strip('"'))
RU,RT,VU,VT=env['UPSTASH_REDIS_REST_URL'],env['UPSTASH_REDIS_REST_TOKEN'],env['UPSTASH_VECTOR_REST_URL'],env['UPSTASH_VECTOR_REST_TOKEN']
assert 'measured-raven' in RU and 'adjusted-iguana' in VU, (RU,VU)
def http(url,data=None,method=None,token=RT):
    req=urllib.request.Request(url,data=json.dumps(data).encode() if data is not None else None,method=method,headers={'Authorization':f'Bearer {token}','Content-Type':'application/json'})
    return json.load(urllib.request.urlopen(req))
def rcmd(*a): return http(RU,list(a))['result']
def rpipe(cmds): return [x.get('result') for x in http(RU+'/pipeline',cmds)]
execute='--execute' in sys.argv
now=datetime.now(timezone.utc)
runs=json.loads(subprocess.run(['gh','run','list','--workflow=source-first-rebuild.yml','--limit','3','--json','status'],cwd=ROOT,capture_output=True,text=True).stdout or '[]')
if any(r['status']!='completed' for r in runs): sys.exit('REFUSING: a rebuild run is in progress')
info=http(VU+'/info',token=VT)['result']; ns=info['namespaces']; before=info['vectorCount']
cur=rcmd('GET','sf:current_generation'); hist=rcmd('GET','sf:generation_history'); hist=json.loads(hist) if isinstance(hist,str) else (hist or [])
keep={cur,*hist}
def age_min(g):
    try: return (now-datetime.strptime(g,'sf_%Y%m%dT%H%M%SZ').replace(tzinfo=timezone.utc)).total_seconds()/60
    except ValueError: return 1e9
orphans=[g for g in ns if g.startswith('sf_') and g not in keep and age_min(g)>=MIN_AGE_MIN]
skipped_young=[g for g in ns if g.startswith('sf_') and g not in keep and age_min(g)<MIN_AGE_MIN]
total=sum(ns[g]['vectorCount'] for g in orphans)
print(f'index {before} vectors; serving {cur}; keep {sorted(keep)}')
print(f'orphans {len(orphans)} = {total} vectors; skipped-as-young {skipped_young}')
print(f'projected after: {before-total} vectors = {(before-total)/660000:.1%} of quota')
log={'ran_at':now.isoformat(),'execute':execute,'before_vectors':before,'keep':sorted(keep),'orphans':{g:ns[g]['vectorCount'] for g in orphans},'skipped_young':skipped_young,'redis_keys_deleted':0,'errors':[]}
if execute:
    for g in orphans:
        # redis keys: sf:<gen>:* plus sf:manifest:<gen>  (mirrors publisher._record_and_prune_generations)
        cursor='0'; keys=[]
        while True:
            c,b=rcmd('SCAN',cursor,'MATCH',f'sf:{g}:*','COUNT','500'); keys+=b; cursor=str(c)
            if cursor=='0': break
        keys.append(f'sf:manifest:{g}')
        for i in range(0,len(keys),100): rpipe([['DEL',*keys[i:i+100]]])
        log['redis_keys_deleted']+=len(keys)
        try: http(f'{VU}/delete-namespace/{g}',method='DELETE',token=VT)
        except Exception as e: log['errors'].append(f'{g}: {e}')
        print('deleted',g,ns[g]['vectorCount'],'vectors,',len(keys),'redis keys')
    after=http(VU+'/info',token=VT)['result']; log['after_vectors']=after['vectorCount']; log['after_namespaces']=sorted(after['namespaces'])
    print(f"AFTER: {after['vectorCount']} vectors = {after['vectorCount']/660000:.1%}; namespaces: {sorted(after['namespaces'])}")
out=HERE/f"orphan_deletion_{now.strftime('%Y%m%dT%H%M%SZ')}{'' if execute else '_dryrun'}.json"; out.write_text(json.dumps(log,indent=1)); print('log ->',out)
