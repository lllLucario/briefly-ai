"""最小独立MS-DOC属性解释；不调用POI SPRM、CLX、FKP或样式解码器。"""
import argparse,bisect,json,struct
from pathlib import Path


def u16(b,p):return struct.unpack_from('<H',b,p)[0]
def u32(b,p):return struct.unpack_from('<I',b,p)[0]


NAMES={0x2416:'in_table',0x2417:'outer_row_end',0x6649:'level',0x664a:'depth_delta',0x244b:'embedded_cell_end',0x244c:'inner_row_end'}
DEFAULT=dict(in_table=False,level=0,outer_row_end=False,embedded_cell_end=False,inner_row_end=False)


def prls(raw,base=0):
    result=[];i=0
    while i+2<=len(raw):
        start=i;op=u16(raw,i);i+=2;size=(1,1,2,4,2,2,None,3)[op>>13]
        if size is None:
            if op==0xd608:
                size=u16(raw,i)-1;i+=2
            elif op==0xc615:
                raise ValueError('PChgTabs exceptional operand not implemented')
            else:size=raw[i];i+=1
        if size<0 or i+size>len(raw):raise ValueError(f'Truncated {op:04x} at {start}')
        value=raw[i:i+size];i+=size
        result.append(dict(offset=base+start,opcode=f'{op:04x}',bytes=raw[start:i].hex(),name=NAMES.get(op),
                           value=int.from_bytes(value,'little',signed=op==0x664a),operand_hex=value.hex()))
    if i<len(raw) and raw[i:]!=b'\0':raise ValueError('Nonzero trailing byte')
    return result


def apply(prls_,initial=None):
    p=dict(DEFAULT if initial is None else initial)
    for r in prls_:
        key=r['name']
        if key=='depth_delta':p['level']+=r['value'];p['in_table']=p['level']!=0
        elif key=='level':p[key]=r['value']
        elif key:p[key]=bool(r['value'])
    p['row_end']=p['outer_row_end'] or p['inner_row_end']
    return p


class Independent:
    def __init__(self,folder):
        self.main=(folder/'WordDocument.bin').read_bytes();self.table=(folder/'Table.bin').read_bytes()
        m=self.main;t=self.table
        off=32;off+=2+u16(m,off)*2;off+=2+u16(m,off)*4;self.fib=off+2
        def field(i):return u32(m,self.fib+8*i),u32(m,self.fib+8*i+4)
        self.field=field
        cp,cl=field(33);end=cp+cl;self.complex=[]
        while t[cp]==1:
            n=u16(t,cp+1);self.complex.append(t[cp+3:cp+3+n].hex());cp+=3+n
        assert t[cp]==2;size=u32(t,cp+1);cp+=5;assert cp+size<=end
        n=(size-4)//12;self.pieces=[]
        for i in range(n):
            p=cp+4*(n+1)+8*i;fc=u32(t,p+2);compressed=bool(fc&0x40000000)
            self.pieces.append(dict(start=u32(t,cp+4*i),end=u32(t,cp+4*(i+1)),fc=(fc&0x3fffffff)//(2 if compressed else 1),bpc=1 if compressed else 2,prm=u16(t,p+6)))
        fc,length=field(13);self.bte_offset=fc;n=(length-4)//8
        self.bte_fcs=[u32(t,fc+4*i) for i in range(n+1)]
        self.pages=[u32(t,fc+4*(n+1)+4*i)&0x3fffff for i in range(n)]
        # STSH -> LPStd -> StdfBase + Xstz + first UPX (paragraph style only).
        fc,length=field(1);cb=u16(t,fc);count=u16(t,fc+2);base=u16(t,fc+4);p=fc+2+cb;self.styles={}
        for i in range(count):
            size=u16(t,p);p+=2;std=p;p+=size
            if not size:continue
            sgc=u16(t,std+2)&15;parent=u16(t,std+2)>>4;cupx=u16(t,std+4)&15
            nameat=std+base;nchars=u16(t,nameat);upx=nameat+2+2*(nchars+1)
            raw=t[upx+2:upx+2+u16(t,upx)] if sgc==1 and cupx else b''
            self.styles[i]=dict(index=i,base=parent,name=t[nameat+2:nameat+2+2*nchars].decode('utf-16le'),
                stream='Table',std_offset=std,std_hex=t[std:std+size].hex(),papx_offset=upx+2,papx_hex=raw.hex())
    def style(self,i,seen=()):
        if i in seen:raise ValueError('Style inheritance cycle')
        s=self.styles[i];chain=[] if s['base']==4095 else self.style(s['base'],seen+(i,))[1]
        chain=chain+[s];state=dict(DEFAULT)
        for item in chain:
            raw=bytes.fromhex(item['papx_hex']);state=apply(prls(raw[2:],item['papx_offset']+2),state)
        return state,chain
    def at(self,cp):
        piece=next(x for x in self.pieces if x['start']<=cp<x['end']);assert piece['prm']==0,'PRM needs independent decoding'
        fc=piece['fc']+(cp-piece['start'])*piece['bpc'];j=bisect.bisect_right(self.bte_fcs,fc)-1
        assert 0<=j<len(self.pages);pn=self.pages[j];page=self.main[pn*512:(pn+1)*512];n=page[511]
        assert 1<=n<=29;fcs=[u32(page,4*i) for i in range(n+1)];k=bisect.bisect_right(fcs,fc)-1;assert 0<=k<n
        def mapped(pos):
            hits={p['start']+(pos-p['fc'])//p['bpc'] for p in self.pieces if p['fc']<=pos<=p['fc']+(p['end']-p['start'])*p['bpc'] and (pos-p['fc'])%p['bpc']==0}
            assert len(hits)==1,('Ambiguous FC mapping',pos,hits)
            return hits.pop()
        start,end=mapped(fcs[k]),mapped(fcs[k+1]);assert start<=cp<end
        bx=4*(n+1)+13*k;bo=2*page[bx]
        if bo==0:raw=b'\0\0';at=pn*512
        else:
            cb=page[bo];at=pn*512+bo+1
            if cb:size=cb*2-1
            else:cb=page[bo+1];size=2*cb;at+=1
            raw=self.main[at:at+size]
        style=u16(raw,0);inherited,chain=self.style(style);direct=prls(raw[2:],at+2);final=apply(direct,inherited)
        return dict(start=start,end=end,cp=cp,fc=fc,piece=piece,source=dict(stream='WordDocument',fkp_page=pn,fkp_index=k,
            bte_table_offset=self.bte_offset,bx_offset=pn*512+bx,rgfc_hex=page[4*k:4*k+8].hex(),papx_offset=at,papx_hex=raw.hex()),
            style=style,style_chain=chain,inherited_properties=inherited,direct_prls=direct,final_properties=final,
            original_terminator_hex=self.main[fcs[k+1]-piece['bpc']:fcs[k+1]].hex())


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('folder',type=Path);folder=parser.parse_args().folder
    reader=Independent(folder/'before');poi=json.loads((folder/'before/poi-paragraphs.json').read_text(encoding='utf-8'));old=json.loads(Path('files/eval/w3/doc-boundaries/object-map/objects.json').read_text(encoding='utf-8'))
    records=[]
    for p in poi['paragraphs']:
        if not 23882<=p['start']<25738:continue
        r=reader.at(p['start']);r['current_paragraph']=p
        r['current_owners']=[x['id'] for x in old['bridge_objects'] if x['start']<=p['start']<x['end']]
        r['different']=[k for k in p['final_properties'] if r['final_properties'][k]!=p['final_properties'][k]]
        text=json.loads(Path('files/eval/w3/doc-boundaries/source-pieces/main-pre-properties.json').read_text(encoding='utf-8'))['text']
        r['internal_text_markers']=[dict(cp=i,code=ord(text[i])) for i in range(r['start'],r['end']) if ord(text[i]) in {7,12,13}]
        records.append(r)
    first=next(r for r in records if r['different'])
    result=dict(records=records,first_difference=first,properties_disagree=sum(bool(r['different']) for r in records),
        independence='FIB, CLX, PCD, BTE, FKP, SPRM and STSH decoded by Python struct; POI only exports unmodified OLE stream bytes and comparison values.',
        limits='Minimal decoder refuses ambiguous FC mapping, nonzero PRMs and exceptional PChgTabs; no use as general production parser.')
    (folder/'independent-properties.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print('audited',len(records),'different',result['properties_disagree'],'first',first['start'],first['end'],first['source'],first['different'])


if __name__=='__main__':main()
