// Read-only native table inventory. User text stays in ignored evidence outputs.
import java.io.*;
import java.nio.file.*;
import java.nio.charset.StandardCharsets;
import java.util.*;
import org.apache.poi.hwpf.HWPFDocument;
import org.apache.poi.hwpf.usermodel.*;

public class HwpfTableProbe {
    static PrintWriter out;
    static int tables=0,maxDepth=0;
    static void walk(Range range,int parent,String path) {
        for(int p=0;p<range.numParagraphs();p++) {
            Paragraph paragraph=range.getParagraph(p);
            if(paragraph.isInTable()&&paragraph.getTableLevel()>parent) {
                Table table=NativeTableAccess.at(range,p,parent+1);tables++;maxDepth=Math.max(maxDepth,table.getTableLevel());
                out.println(path+"\tTABLE\t"+table.getTableLevel()+"\t"+table.numRows()+"\t"+table.getStartOffset()+"\t"+table.getEndOffset());
                for(int r=0;r<table.numRows();r++)for(int c=0;c<table.getRow(r).numCells();c++)
                    walk(table.getRow(r).getCell(c),table.getTableLevel(),path+"/t"+tables+"r"+r+"c"+c);
                while(p+1<range.numParagraphs()&&range.getParagraph(p+1).getStartOffset()<table.getEndOffset())p++;
            }
        }
    }
    public static void main(String[] args) throws Exception {
        try(InputStream in=Files.newInputStream(Path.of(args[0]));HWPFDocument doc=new HWPFDocument(in);
            PrintWriter writer=new PrintWriter(Files.newBufferedWriter(Path.of(args[1]),StandardCharsets.UTF_8))) {
            out=writer;
            TreeMap<Integer,Integer> levels=new TreeMap<>();Range range=doc.getRange();
            for(int p=0;p<range.numParagraphs();p++) {Paragraph paragraph=range.getParagraph(p);if(paragraph.isInTable())levels.merge(paragraph.getTableLevel(),1,Integer::sum);}
            System.out.println("paragraphs="+range.numParagraphs()+" levels="+levels);
            walk(range,0,"body");
            System.out.println("tables="+tables+" maxDepth="+maxDepth);
        }
    }
}
