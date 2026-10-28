// Read-only HWPF story ranges; no production parser or macro execution.
import java.io.*;
import java.nio.charset.StandardCharsets;
import java.nio.file.*;
import java.util.*;
import org.apache.poi.hwpf.HWPFDocument;
import org.apache.poi.hwpf.model.SubdocumentType;
import org.apache.poi.hwpf.usermodel.Range;

public class WordRangeProbe {
    static String quote(String text){
        StringBuilder result=new StringBuilder("\"");
        for(int i=0;i<text.length();i++){
            char c=text.charAt(i);
            if(c=='"'||c=='\\')result.append('\\').append(c);
            else if(c<32||c>126)result.append(String.format(Locale.ROOT,"\\u%04x",(int)c));
            else result.append(c);
        }
        return result.append('"').toString();
    }
    public static void main(String[] args)throws Exception{
        Path output=Path.of(args[1]);Files.createDirectories(output);
        try(InputStream in=Files.newInputStream(Path.of(args[0]));HWPFDocument doc=new HWPFDocument(in)){
            int start=0;List<String> records=new ArrayList<>();
            for(SubdocumentType type:SubdocumentType.ORDERED){
                int count=doc.getFileInformationBlock().getSubdocumentTextStreamLength(type);
                Range range=new Range(start,start+count,doc);
                records.add(quote(type.name().toLowerCase(Locale.ROOT))+": {\"start\":"+start+",\"end\":"+(start+count)+",\"text\":"+quote(range.text())+"}");
                System.out.println(type.name()+" chars="+count);
                start+=count;
            }
            Files.writeString(output.resolve("ranges-raw.json"),"{\n"+String.join(",\n",records)+"\n}",StandardCharsets.UTF_8);
        }
    }
}
