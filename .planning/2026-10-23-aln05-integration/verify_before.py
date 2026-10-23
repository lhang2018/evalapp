"""Run only public-store/manifest probes against the untouched pre-integration commit."""
import io,os,shutil,subprocess,sys,zipfile
from pathlib import Path
root=Path(__file__).resolve().parents[2]
evidence=Path(__file__).resolve().parent
commit=subprocess.check_output(["git","rev-parse","04de43f"],cwd=root,text=True).strip()
snapshot=root/"outputs"/"aln05-before"
snapshot.mkdir(exist_ok=False)
with zipfile.ZipFile(io.BytesIO(subprocess.check_output(["git","archive","--format=zip",commit],cwd=root))) as archive:
    archive.extractall(snapshot)
shutil.copyfile(root/"tests/test_memory_owner_partition.py",snapshot/"tests/test_memory_owner_partition.py")
result=subprocess.run([sys.executable,"-m","pytest","tests/test_memory_owner_partition.py","-q","--tb=short"],
    cwd=snapshot,capture_output=True,env={**os.environ,"PYTHONIOENCODING":"utf-8","PYTHONUTF8":"1"})
text=(result.stdout+result.stderr).decode("utf-8").replace("\r\n","\n")
(evidence/"before-fixed.txt").write_text(text,encoding="utf-8",newline="\n")
print(text[-1000:])
assert result.returncode==1
