// Diagnostic only. Original streams retained for a POI-independent Python decoder.
import java.io.*;
import java.nio.file.*;
import java.nio.charset.StandardCharsets;
import java.util.*;
import org.apache.poi.hwpf.HWPFDocument;
import org.apache.poi.hwpf.model.*;
import org.apache.poi.hwpf.usermodel.*;

public class DocParagraphEvidenceProbe {
    static Map<String,Object> props(ParagraphProperties p){return DocObjectMapProbe.map("in_table",p.getFInTable(),"level",p.getItap(),"row_end",p.getFTtp()||p.getFTtpEmbedded(),"inner_row_end",p.getFTtpEmbedded(),"embedded_cell_end",p.getFInnerTableCell());}
    public static void main(String[] args)throws Exception{
        Path out=Path.of(args[1]);Files.createDirectories(out);
        try(InputStream in=Files.newInputStream(Path.of(args[0]));HWPFDocument d=new HWPFDocument(in)){
            if(args.length>2&&args[2].equals("restore"))LegacyDocParagraphs.restore(d);
            Files.write(out.resolve("WordDocument.bin"),d.getMainStream());Files.write(out.resolve("Table.bin"),d.getTableStream());Files.write(out.resolve("Data.bin"),d.getDataStream());
            List<Object> pars=new ArrayList<>(),styles=new ArrayList<>();Range range=d.getRange();
            for(PAPX p:d.getParagraphTable().getParagraphs()){
                if(p.getEnd()<=23800||p.getStart()>=25800)continue;
                Map<String,Object> r=DocObjectMapProbe.map("start",p.getStart(),"end",p.getEnd(),"style",p.getIstd(),"rebuilt_grpprl_hex",HexFormat.of().formatHex(p.getGrpprl()),"final_properties",props(Paragraph.newParagraph(range,p).getProps()),"text",Paragraph.newParagraph(range,p).text());
                pars.add(r);
            }
            for(int i=0;i<d.getStyleSheet().numStyles();i++){
                StyleDescription s=d.getStyleSheet().getStyleDescription(i);if(s==null)continue;
                styles.add(DocObjectMapProbe.map("index",i,"name",s.getName(),"base",s.getBaseStyle(),"papx_hex",s.getPAPX()==null?"":HexFormat.of().formatHex(s.getPAPX()),"resolved_properties",props(d.getStyleSheet().getParagraphStyle(i))));
            }
            Files.writeString(out.resolve("poi-paragraphs.json"),DocObjectMapProbe.json(DocObjectMapProbe.map("paragraphs",pars,"styles",styles)).toString(),StandardCharsets.UTF_8);
        }
    }
}
