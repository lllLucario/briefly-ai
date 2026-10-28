// Same-HWPF debugging reference only; never a production fallback.
import java.io.*;
import java.nio.charset.StandardCharsets;
import java.nio.file.*;
import java.util.*;
import javax.xml.parsers.DocumentBuilderFactory;
import javax.xml.transform.*;
import javax.xml.transform.dom.DOMSource;
import javax.xml.transform.stream.StreamResult;
import org.apache.poi.hwpf.HWPFDocument;
import org.apache.poi.hwpf.HWPFDocumentCore;
import org.apache.poi.hwpf.converter.WordToHtmlConverter;
import org.apache.poi.hwpf.model.FieldsDocumentPart;
import org.apache.poi.hwpf.usermodel.*;
import org.w3c.dom.*;

public class PoiHtmlReferenceProbe {
    static class Probe extends WordToHtmlConverter {
        final PrintWriter visits;
        Probe(Document document,PrintWriter visits){super(document);this.visits=visits;}
        @Override protected void processTable(HWPFDocumentCore doc,Element flow,Table table){
            int cells=0;for(int r=0;r<table.numRows();r++)cells+=table.getRow(r).numCells();
            visits.println(table.getStartOffset()+"\t"+table.getEndOffset()+"\t"+table.getTableLevel()+"\t"+table.numRows()+"\t"+cells);visits.flush();
            Node previousLast=flow.getLastChild();
            super.processTable(doc,flow,table);
            for(Node n=flow.getLastChild();n!=null&&n!=previousLast;n=n.getPreviousSibling()){
                if(n instanceof Element&&((Element)n).getTagName().equals("table")){
                    Element html=(Element)n;
                    html.setAttribute("data-probe-source-start",Integer.toString(table.getStartOffset()));
                    html.setAttribute("data-probe-source-end",Integer.toString(table.getEndOffset()));
                    html.setAttribute("data-probe-source-level",Integer.toString(table.getTableLevel()));break;
                }
            }
        }
        void local(HWPFDocument doc,Table table){processTable(doc,(Element)getDocument().getElementsByTagName("body").item(0),table);}
    }
    static void write(Document document,Path path)throws Exception{
        Transformer transformer=TransformerFactory.newInstance().newTransformer();
        transformer.setOutputProperty(OutputKeys.METHOD,"xml");transformer.setOutputProperty(OutputKeys.ENCODING,"UTF-8");
        try(OutputStream out=Files.newOutputStream(path)){transformer.transform(new DOMSource(document),new StreamResult(out));}
    }
    public static void main(String[] args)throws Exception{
        Path folder=Path.of(args[1]);Files.createDirectories(folder);
        System.out.println("POI="+org.apache.poi.Version.getVersion());
        try(InputStream in=Files.newInputStream(Path.of(args[0]));HWPFDocument doc=new HWPFDocument(in)){
            try(PrintWriter fields=new PrintWriter(Files.newBufferedWriter(folder.resolve("local-fields.tsv"),StandardCharsets.UTF_8))){
                for(Field field:doc.getFields().getFields(FieldsDocumentPart.MAIN)){
                    if(field.getFieldStartOffset()<22000||field.getFieldStartOffset()>26000)continue;
                    Range instruction=field.firstSubrange(doc.getRange());
                    fields.println(field.getFieldStartOffset()+"\t"+field.getFieldEndOffset()+"\t"+field.getType()+"\t"+WordRangeProbe.quote(instruction==null?"":instruction.text()));
                }
            }
            try(PrintWriter visits=new PrintWriter(Files.newBufferedWriter(folder.resolve("full-visits.tsv"),StandardCharsets.UTF_8))){
                Probe converter=new Probe(DocumentBuilderFactory.newInstance().newDocumentBuilder().newDocument(),visits);
                try{converter.processDocument(doc);Files.writeString(folder.resolve("full.status"),"completed");}
                catch(Exception error){Files.writeString(folder.resolve("full.status"),"failed: "+error.toString());}
                write(converter.getDocument(),folder.resolve("full.xhtml"));
            }
            Set<Integer> wanted=new LinkedHashSet<>(Arrays.asList(23882,25283,25325,25434,25446,25495,25518,25528));
            Range range=doc.getRange();
            for(int p=0;p<range.numParagraphs();p++){
                Paragraph paragraph=range.getParagraph(p);
                int start=paragraph.getStartOffset();if(!wanted.contains(start))continue;
                Path output=folder.resolve("local-"+start);Files.createDirectories(output);
                try(PrintWriter visits=new PrintWriter(Files.newBufferedWriter(output.resolve("visits.tsv"),StandardCharsets.UTF_8))){
                    Probe converter=new Probe(DocumentBuilderFactory.newInstance().newDocumentBuilder().newDocument(),visits);
                    try{
                        Table table=range.getTable(paragraph);converter.local(doc,table);
                        Files.writeString(output.resolve("status.txt"),"completed: native_rows="+table.numRows());
                    }catch(Exception error){Files.writeString(output.resolve("status.txt"),"failed: "+error.toString());}
                    write(converter.getDocument(),output.resolve("output.xhtml"));
                }
            }
        }
    }
}
