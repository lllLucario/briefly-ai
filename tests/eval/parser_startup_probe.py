"""拆开模块导入/转换器创建和实际解析；诊断一次，不充当P95。"""
import argparse
import importlib
from pathlib import Path
from parser_resource_probe import measure, save


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('source',type=Path)
    parser.add_argument('output',type=Path)
    args = parser.parse_args()
    payload = args.source.read_bytes()
    steps = []
    loaders, stats = measure(lambda:importlib.import_module('briefly.loaders'))
    steps.append(dict(step='loaders_import',**stats))
    kind = args.source.suffix
    module = {'.docx':'docling.datamodel.base_models','.xlsx':'openpyxl','.pdf':'briefly.pdf'}[kind]
    _, stats = measure(lambda:importlib.import_module(module))
    steps.append(dict(step=module+'_import',**stats))
    if kind == '.docx':
        _, stats = measure(loaders._docling_converter)
        steps.append(dict(step='converter_factory_first',**stats))
        _, stats = measure(loaders._docling_converter)
        steps.append(dict(step='converter_factory_reuse',**stats))
    for repeat in range(2):
        doc, stats = measure(lambda:loaders.load_document(args.source.name,payload))
        assert 'PROBE-END-7391' in doc.text
        steps.append(dict(step=f'parse_after_init_{repeat}',**stats))
    save(args.output,steps)


if __name__=='__main__':
    main()
