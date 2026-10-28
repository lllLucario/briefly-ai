"""正式直接解析入口的单文件新JVM耗时，不混同批量均摊耗时。"""
import argparse
from pathlib import Path

from briefly.direct_word import parse_docs
from doc_parser_comparison import measured,save


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('source',type=Path)
    parser.add_argument('output',type=Path)
    args=parser.parse_args()
    data=args.source.read_bytes()
    results=[]
    for repeat in range(1,4):
        value,stats=measured(lambda:parse_docs([data]))
        assert not isinstance(value[0],Exception),value[0]
        assert '| 杭州 | 正式员工 | 480元 |' in value[0][0]
        results.append(dict(repeat=repeat,stats=stats))
    save(args.output,results)
    print('Single-file probe passed',flush=True)


if __name__=='__main__':main()
