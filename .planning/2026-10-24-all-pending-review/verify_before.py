import hashlib,io,json,os,subprocess,sys,zipfile
from pathlib import Path
root=Path(__file__).resolve().parents[2]; folder=Path(__file__).resolve().parent
ref=subprocess.check_output(["git","rev-parse","08d2ac9"],cwd=root,text=True).strip()
snapshot=root/"outputs"/("all-pending-before-"+ref[:7]); snapshot.mkdir(exist_ok=False)
with zipfile.ZipFile(io.BytesIO(subprocess.check_output(["git","archive","--format=zip",ref],cwd=root))) as z: z.extractall(snapshot)
probes=["tests/test_peer_runtime_protocol_review.py","android-app/test/device/peerTaskProtocol.test.ts","android-app/test/update/peerBundleReview.test.ts"]
for probe in probes: (snapshot/probe).write_bytes((root/probe).read_bytes())
env={**os.environ,"PYTHONIOENCODING":"utf-8","PYTHONUTF8":"1"}
results=[]
for name,command in [("runtime-before-committed",[sys.executable,"-m","pytest",probes[0],"-q","--tb=short"]),("client-before-committed",["node","--test",*probes[1:]])]:
 log=folder/(name+".txt")
 with log.open("w",encoding="utf-8") as f: result=subprocess.run(command,cwd=snapshot,env=env,stdout=f,stderr=subprocess.STDOUT)
 log.write_text(log.read_text(encoding="utf-8"),encoding="utf-8",newline="\n")
 assert result.returncode!=0
 results.append(dict(name=name,command=command,exit_code=result.returncode,log=log.relative_to(root).as_posix(),sha256=hashlib.sha256(log.read_bytes()).hexdigest()))
 print(name,log.read_text(encoding="utf-8")[-250:])
record=dict(source_commit=ref,probes=[dict(path=f,sha256=hashlib.sha256((root/f).read_bytes()).hexdigest()) for f in probes],results=results)
(folder/"before-evidence.json").write_text(json.dumps(record,indent=2)+"\n",encoding="utf-8",newline="\n")
