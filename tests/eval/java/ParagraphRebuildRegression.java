// Original minimal reproduction, contains no user document content.
import java.util.*;
import org.apache.poi.hwpf.model.*;
import org.apache.poi.hwpf.sprm.*;

public class ParagraphRebuildRegression {
    static void check(boolean value){if(!value)throw new AssertionError("Paragraph repair guard failed");}
    static PAPX p(int start,int end,String hex){return new PAPX(start,end,new SprmBuffer(HexFormat.of().parseHex(hex),2));}
    public static void main(String[] args){
        String raw="00001624014966020000004b2401";
        PAPX original=p(0,3,raw);PAPBinTable table=new PAPBinTable();table.getParagraphs().add(original);
        StringBuilder text=new StringBuilder("A\r\r");ComplexFileTable cft=new ComplexFileTable();cft.getTextPieceTable().add(new SinglentonTextPiece(text));
        table.rebuild(text,cft);List<PAPX> split=table.getParagraphs();
        check(split.size()==2);check(split.get(0).getStart()==0&&split.get(0).getEnd()==2);
        check(!LegacyDocParagraphs.explicitNestedCell(split.get(0)));
        check(LegacyDocParagraphs.explicitNestedCell(original));check(LegacyDocParagraphs.exactSplit(original,split));
        check(!LegacyDocParagraphs.exactSplit(original,List.of(p(0,2,"0000032401"),p(2,3,raw))));
        check(!LegacyDocParagraphs.exactSplit(original,List.of(p(0,1,"000000000000"),p(2,3,raw))));
        check(!LegacyDocParagraphs.exactSplit(original,List.of(p(0,2,"000000000000"),p(2,4,raw))));
        check(!LegacyDocParagraphs.explicitNestedCell(p(0,3,"00001624014966010000004b2401")));
        check(!LegacyDocParagraphs.explicitNestedCell(p(0,3,raw+"4a6601000000")));
        System.out.println("10 minimal reproduction and positive-evidence guard checks passed");
    }
}
