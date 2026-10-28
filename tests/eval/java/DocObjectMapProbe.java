// Diagnostic only: source FKP properties -> natural HWPF objects -> existing bridge output.
import java.io.*;
import java.nio.charset.StandardCharsets;
import java.nio.file.*;
import java.util.*;
import javax.xml.parsers.DocumentBuilderFactory;
import javax.xml.transform.*;
import javax.xml.transform.dom.DOMSource;
import javax.xml.transform.stream.StreamResult;
import org.apache.poi.hwpf.HWPFDocument;
import org.apache.poi.hwpf.model.*;
import org.apache.poi.hwpf.usermodel.*;
import org.apache.poi.util.LittleEndian;
import org.w3c.dom.*;

public class DocObjectMapProbe {
    static Object json(Object v){
        if(v instanceof String)return WordRangeProbe.quote((String)v);
        if(v instanceof Map){List<String> parts=new ArrayList<>();for(Object key:((Map<?,?>)v).keySet())parts.add(WordRangeProbe.quote(key.toString())+":"+json(((Map<?,?>)v).get(key)));return "{"+String.join(",",parts)+"}";}
        if(v instanceof List){List<String> parts=new ArrayList<>();for(Object x:(List<?>)v)parts.add(json(x).toString());return "["+String.join(",",parts)+"]";}
        return String.valueOf(v);
    }
    static Map<String,Object> map(Object... values){Map<String,Object> m=new LinkedHashMap<>();for(int i=0;i<values.length;i+=2)m.put(values[i].toString(),values[i+1]);return m;}
    static Map<String,Object> paragraph(Paragraph p){return map("start",p.getStartOffset(),"end",p.getEndOffset(),"in_table",p.isInTable(),"level",p.getTableLevel(),"row_end",p.isTableRowEnd(),"embedded_cell_end",p.isEmbeddedCellMark(),"raw_text",p.text());}
    static String ownText(TableCell cell,int level){StringBuilder s=new StringBuilder();for(int p=0;p<cell.numParagraphs();p++){Paragraph para=cell.getParagraph(p);if(para.getTableLevel()<=level)s.append(para.text());}return s.toString();}
    static Map<String,Object> tableRecord(Table table,String id,String parent){
        List<Object> rows=new ArrayList<>();
        for(int r=0;r<table.numRows();r++){
            TableRow row=table.getRow(r);List<Object> cells=new ArrayList<>();
            for(int c=0;c<row.numCells();c++){
                TableCell cell=row.getCell(c);
                cells.add(map("id",id+"/r"+r+"c"+c,"row",r,"cell",c,"start",cell.getStartOffset(),"end",cell.getEndOffset(),"left",cell.getLeftEdge(),"width",cell.getWidth(),"horizontal_merged",cell.isMerged(),"horizontal_first",cell.isFirstMerged(),"vertical_merged",cell.isVerticallyMerged(),"vertical_first",cell.isFirstVerticallyMerged(),"own_text",ownText(cell,table.getTableLevel())));
            }
            rows.add(map("row",r,"start",row.getStartOffset(),"end",row.getEndOffset(),"cells",cells));
        }
        return map("id",id,"start",table.getStartOffset(),"end",table.getEndOffset(),"level",table.getTableLevel(),"parent_cell",parent,"rows",rows,"raw_text",table.text());
    }
    static void natural(Range range,int parentLevel,String parent,List<Object> result){
        for(int p=0;p<range.numParagraphs();p++){
            Paragraph para=range.getParagraph(p);
            if(!para.isInTable()||para.getTableLevel()<=parentLevel)continue;
            Table table=range.getTable(para);String id="H:"+table.getStartOffset()+":"+table.getEndOffset()+":"+table.getTableLevel();
            result.add(tableRecord(table,id,parent));
            for(int r=0;r<table.numRows();r++)for(int c=0;c<table.getRow(r).numCells();c++)natural(table.getRow(r).getCell(c),table.getTableLevel(),id+"/r"+r+"c"+c,result);
            while(p+1<range.numParagraphs()&&range.getParagraph(p+1).getStartOffset()<table.getEndOffset())p++;
        }
    }
    static class Context {String id,cell="";Table table;Context(String id,Table table){this.id=id;this.table=table;}}
    static class Trace extends LegacyDocStructured.NativeState {
        final List<Object> records=new ArrayList<>();final Deque<Context> stack=new ArrayDeque<>();
        Trace(Document d){super(d);}
        @Override Node table(Table table,int depth)throws LegacyDocStructured.UnsupportedStructure{
            String id="J:"+table.getStartOffset()+":"+table.getEndOffset()+":"+table.getTableLevel();
            Map<String,Object> record=tableRecord(table,id,stack.isEmpty()?"":stack.peek().cell);records.add(record);
            stack.push(new Context(id,table));Node result;
            try{result=super.table(table,depth);}finally{stack.pop();}
            Element element=(Element)result;element.setAttribute("data-probe-id",id);element.setAttribute("data-probe-source-start",Integer.toString(table.getStartOffset()));
            record.put("render_kind",element.getTagName());return result;
        }
        @Override void appendCell(Element target,TableCell cell,int level,int depth)throws LegacyDocStructured.UnsupportedStructure{
            Context current=stack.peek();String previous=current.cell;
            outer:for(int r=0;r<current.table.numRows();r++)for(int c=0;c<current.table.getRow(r).numCells();c++)if(current.table.getRow(r).getCell(c)==cell){current.cell=current.id+"/r"+r+"c"+c;break outer;}
            target.setAttribute("data-probe-cell",current.cell);
            try{super.appendCell(target,cell,level,depth);}finally{current.cell=previous;}
        }
    }
    public static void main(String[] args)throws Exception{
        Path output=Path.of(args[1]);Files.createDirectories(output);
        try(InputStream in=Files.newInputStream(Path.of(args[0]));HWPFDocument doc=new HWPFDocument(in)){
            if(args.length>2&&args[2].equals("restore"))LegacyDocParagraphs.restore(doc);
            Range range=doc.getRange();List<Object> paragraphs=new ArrayList<>();
            for(int p=0;p<range.numParagraphs();p++)paragraphs.add(paragraph(range.getParagraph(p)));
            List<Object> rawProperties=new ArrayList<>();byte[] main=doc.getMainStream(),data=doc.getDataStream(),table=doc.getTableStream();
            // HWPFDocument replaces its text piece table after rebuilding paragraphs.
            // FKP byte positions must use the ORIGINAL CLX translator, not that singleton.
            TextPieceTable originalPieces=new ComplexFileTable(main,table,doc.getFileInformationBlock().getFcClx(),0).getTextPieceTable();
            int fc=doc.getFileInformationBlock().getFcPlcfbtePapx(),length=doc.getFileInformationBlock().getLcbPlcfbtePapx();int n=(length-4)/8;
            for(int i=0;i<n;i++){
                int page=LittleEndian.getInt(table,fc+4*(n+1)+4*i);
                PAPFormattedDiskPage fkp=new PAPFormattedDiskPage(main,data,page*512,originalPieces);boolean relevant=false;
                for(PAPX pap:fkp.getPAPXs()){
                    if(pap.getEnd()<23000||pap.getStart()>27000)continue;relevant=true;
                    Map<String,Object> record=paragraph(Paragraph.newParagraph(range,pap));record.put("source_fkp_page",page);record.put("grpprl_hex",HexFormat.of().formatHex(pap.getGrpprl()));rawProperties.add(record);
                }
                if(relevant)Files.write(output.resolve("fkp-page-"+page+".bin"),Arrays.copyOfRange(main,page*512,(page+1)*512));
            }
            List<Object> natural=new ArrayList<>();natural(range,0,"",natural);
            Document dom=DocumentBuilderFactory.newInstance().newDocumentBuilder().newDocument();Element html=dom.createElement("html");dom.appendChild(html);html.appendChild(dom.createElement("head"));Element body=dom.createElement("body");html.appendChild(body);Trace trace=new Trace(dom);
            for(int p=0;p<range.numParagraphs();p++){
                if(!range.getParagraph(p).isInTable())continue;
                Table root=NativeTableAccess.at(range,p,1);body.appendChild(trace.table(root,0));
                while(p+1<range.numParagraphs()&&range.getParagraph(p+1).getStartOffset()<root.getEndOffset())p++;
            }
            Files.writeString(output.resolve("objects.json"),json(map("raw_fkp_properties",rawProperties,"hwpf_paragraphs",paragraphs,"natural_hwpf_tables",natural,"bridge_objects",trace.records)).toString(),StandardCharsets.UTF_8);
            Transformer tr=TransformerFactory.newInstance().newTransformer();tr.setOutputProperty(OutputKeys.METHOD,"xml");tr.setOutputProperty(OutputKeys.ENCODING,"UTF-8");tr.transform(new DOMSource(dom),new StreamResult(output.resolve("bridge-tables.xhtml").toFile()));
            System.out.println("natural="+natural.size()+" bridge="+trace.records.size()+" source_fkp="+rawProperties.size());
        }
    }
}
