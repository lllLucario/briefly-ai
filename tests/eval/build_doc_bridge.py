"""本地构建自有Java桥接jar，不联网。需要已准备的Java/Tika及ECJ编译器。"""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
from zipfile import ZipFile,ZIP_DEFLATED


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--java',required=True)
    parser.add_argument('--tika',required=True)
    parser.add_argument('--compiler',required=True)
    args=parser.parse_args()
    folder=Path(__file__).resolve().parents[2]/'src/briefly/java'
    sources=sorted(folder.glob('*.java'))
    with TemporaryDirectory(prefix='briefly-doc-build-') as temp:
        subprocess.run([args.java,'-jar',args.compiler,'-17','-encoding','UTF-8','-cp',args.tika,'-d',temp,*map(str,sources)],check=True)
        target=folder/'legacy-doc-parser.jar'
        with ZipFile(target,'w',ZIP_DEFLATED) as archive:
            for path in sorted(Path(temp).rglob('*.class')):
                archive.write(path,path.relative_to(temp).as_posix())
    sha=lambda p:hashlib.sha256(p.read_bytes()).hexdigest()
    metadata=dict(target_java=17,sources={p.name:sha(p) for p in sources},jar_sha256=sha(target),
                  compiler_sha256=sha(Path(args.compiler)),tika_sha256=sha(Path(args.tika)))
    (folder/'build.json').write_text(json.dumps(metadata,indent=2)+'\n',encoding='utf-8')
    print('Built local bridge',target.stat().st_size,'bytes')


if __name__=='__main__':main()
