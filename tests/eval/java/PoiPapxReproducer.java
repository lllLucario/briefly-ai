// Standalone, original synthetic reproduction. No Briefly classes or user documents.
import java.util.HexFormat;
import org.apache.poi.hwpf.model.*;
import org.apache.poi.hwpf.sprm.*;
import org.apache.poi.hwpf.usermodel.ParagraphProperties;

public class PoiPapxReproducer {
    public static void main(String[] args) {
        StringBuilder text=new StringBuilder("A\r\r");
        PAPBinTable table=new PAPBinTable();
        table.getParagraphs().add(new PAPX(0,3,new SprmBuffer(HexFormat.of().parseHex("00001624014966020000004b2401"),2)));
        ComplexFileTable cft=new ComplexFileTable();
        cft.getTextPieceTable().add(new SinglentonTextPiece(text));
        table.rebuild(text,cft);
        for(PAPX p:table.getParagraphs()) {
            ParagraphProperties props=ParagraphSprmUncompressor.uncompressPAP(new ParagraphProperties(),p.getGrpprl(),2);
            System.out.printf("[%d,%d) inTable=%s depth=%d innerCell=%s bytes=%s%n",p.getStart(),p.getEnd(),props.getFInTable(),props.getItap(),props.getFInnerTableCell(),HexFormat.of().formatHex(p.getGrpprl()));
        }
    }
}
