// Experimental correction using native HWPF cell geometry/merge flags, never text guesses.
import java.io.*;
import java.nio.file.*;
import java.util.*;
import javax.xml.parsers.*;
import javax.xml.transform.*;
import javax.xml.transform.dom.DOMSource;
import javax.xml.transform.stream.StreamResult;
import org.w3c.dom.*;
import org.apache.poi.hwpf.HWPFDocument;
import org.apache.poi.hwpf.usermodel.*;

public class DocStructured {
    public static void main(String[] args) throws Exception {
        DocProbe.main(args);
        for (int i=1;i<args.length;i++) {
            Path target=Path.of(args[0]).resolve((i-1)+".xhtml");
            DocumentBuilderFactory factory=DocumentBuilderFactory.newInstance();
            factory.setFeature("http://apache.org/xml/features/disallow-doctype-decl",true);
            factory.setFeature("http://xml.org/sax/features/external-general-entities",false);
            factory.setFeature("http://xml.org/sax/features/external-parameter-entities",false);
            org.w3c.dom.Document output=factory.newDocumentBuilder().parse(target.toFile());
            List<Node> htmlTables=new ArrayList<>();
            NodeList nodes=output.getElementsByTagName("table");
            for (int n=0;n<nodes.getLength();n++)htmlTables.add(nodes.item(n));
            try(InputStream in=Files.newInputStream(Path.of(args[i]));HWPFDocument doc=new HWPFDocument(in)) {
                List<Table> tables=new ArrayList<>();
                Range range=doc.getRange();
                for(int p=0;p<range.numParagraphs();p++) {
                    Paragraph paragraph=range.getParagraph(p);
                    if(paragraph.isInTable()) {
                        Table found=range.getTable(paragraph);
                        tables.add(found);
                        while(p+1<range.numParagraphs()&&range.getParagraph(p+1).getStartOffset()<found.getEndOffset())p++;
                    }
                }
                if(tables.size()!=htmlTables.size())throw new IOException("Table mapping mismatch");
                for(int t=0;t<tables.size();t++) {
                    Table table=tables.get(t);
                    TreeSet<Integer> grid=new TreeSet<>();
                    for(int r=0;r<table.numRows();r++)for(int c=0;c<table.getRow(r).numCells();c++) {
                        TableCell cell=table.getRow(r).getCell(c);
                        grid.add(cell.getLeftEdge());grid.add(cell.getLeftEdge()+cell.getWidth());
                    }
                    Element replacement=output.createElement("table");
                    Element tbody=output.createElement("tbody");replacement.appendChild(tbody);
                    for(int r=0;r<table.numRows();r++) {
                        TableRow row=table.getRow(r);
                        Element tr=output.createElement("tr");tbody.appendChild(tr);
                        for(int c=0;c<row.numCells();c++) {
                            TableCell cell=row.getCell(c);
                            if(cell.isVerticallyMerged()&&!cell.isFirstVerticallyMerged())continue;
                            if(cell.isMerged()&&!cell.isFirstMerged())continue;
                            Element td=output.createElement("td");
                            int span=0;
                            for(int point:grid)if(point>=cell.getLeftEdge()&&point<cell.getLeftEdge()+cell.getWidth())span++;
                            if(span>1)td.setAttribute("colspan",Integer.toString(span));
                            if(cell.isFirstVerticallyMerged()) {
                                int rowspan=1;
                                outer:for(int rr=r+1;rr<table.numRows();rr++) {
                                    TableRow next=table.getRow(rr);
                                    for(int cc=0;cc<next.numCells();cc++) {
                                        TableCell candidate=next.getCell(cc);
                                        if(candidate.getLeftEdge()==cell.getLeftEdge()&&candidate.getWidth()==cell.getWidth()&&candidate.isVerticallyMerged()&&!candidate.isFirstVerticallyMerged()) {
                                            rowspan++;continue outer;
                                        }
                                    }
                                    break;
                                }
                                if(rowspan>1)td.setAttribute("rowspan",Integer.toString(rowspan));
                            }
                            td.setTextContent(cell.text().replace('\u0007',' ').replace('\r',' ').trim());
                            tr.appendChild(td);
                        }
                    }
                    Node old=htmlTables.get(t);old.getParentNode().replaceChild(replacement,old);
                }
            }
            Transformer transformer=TransformerFactory.newInstance().newTransformer();
            transformer.setOutputProperty(OutputKeys.ENCODING,"UTF-8");
            try(OutputStream out=Files.newOutputStream(target)){transformer.transform(new DOMSource(output),new StreamResult(out));}
        }
    }
}
