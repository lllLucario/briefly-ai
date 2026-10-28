// Native HWPF tables, including nested tables; keep uncertain segments visible.
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

public class LegacyDocStructured {
    public static class UnsupportedStructure extends IOException {
        private static final long serialVersionUID = 1L;
        public UnsupportedStructure(String message) { super(message); }
    }
    public static void main(String[] args) throws Exception {
        for (int i=1;i<args.length;i++) {
            try (InputStream source=Files.newInputStream(Path.of(args[i])); HWPFDocument document=new HWPFDocument(source);
                 org.apache.poi.hwpf.extractor.WordExtractor extractor=new org.apache.poi.hwpf.extractor.WordExtractor(document)) {
                if(Arrays.stream(extractor.getCommentsText()).anyMatch(s->!s.isBlank()))
                    throw new UnsupportedStructure("Comments require separate review");
            }
        }
        LegacyDocBase.parse(args,false);
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
            try(InputStream in=Files.newInputStream(Path.of(args[i]));HWPFDocument doc=new HWPFDocument(in);
                org.apache.poi.hwpf.extractor.WordExtractor extractor=new org.apache.poi.hwpf.extractor.WordExtractor(doc)) {
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
                if(tables.size()!=htmlTables.size())throw new UnsupportedStructure("Table mapping mismatch");
                Element count=output.createElement("meta");
                count.setAttribute("name","briefly:footnotes");
                count.setAttribute("content",Long.toString(Arrays.stream(extractor.getFootnoteText()).filter(s->!s.isBlank()).count()));
                output.getElementsByTagName("head").item(0).appendChild(count);
                NativeState state=new NativeState(output);
                int restoredParagraphs=LegacyDocParagraphs.restore(doc);
                range=doc.getRange();
                state.meta("briefly:restored-source-paragraphs",restoredParagraphs);
                List<Table> roots=new ArrayList<>();
                for(int p=0;p<range.numParagraphs();p++){
                    if(!range.getParagraph(p).isInTable())continue;
                    Table root=NativeTableAccess.at(range,p,1);roots.add(root);
                    while(p+1<range.numParagraphs()&&range.getParagraph(p+1).getStartOffset()<root.getEndOffset())p++;
                }
                int previous=0;
                for(Table root:roots){
                    int first=previous;
                    while(previous<tables.size()&&tables.get(previous).getStartOffset()<root.getEndOffset())previous++;
                    if(first==previous)throw new UnsupportedStructure("Native root has no XHTML source anchor");
                    Node old=htmlTables.get(first),last=htmlTables.get(previous-1);
                    if(old.getParentNode()!=last.getParentNode())throw new UnsupportedStructure("Non-sibling source table anchors");
                    Node parent=old.getParentNode(),after=last.getNextSibling();
                    parent.insertBefore(state.table(root,0),old);
                    while(old!=after){Node next=old.getNextSibling();parent.removeChild(old);old=next;}
                }
                if(previous!=tables.size())throw new UnsupportedStructure("Unmapped source table anchors");
                state.meta("briefly:tables",state.tables);
                state.meta("briefly:nested-tables",state.nested);
                state.meta("briefly:unverified-table-segments",state.unverified);
                int objects=0;
                String raw=doc.getRange().text();
                for(int c=0;c<raw.length();c++)if(raw.charAt(c)==1||raw.charAt(c)==8)objects++;
                state.meta("briefly:object-anchors",objects);
                LegacyDocTextboxes.append(output,doc,state);
            }
            Transformer transformer=TransformerFactory.newInstance().newTransformer();
            transformer.setOutputProperty(OutputKeys.ENCODING,"UTF-8");
            try(OutputStream out=Files.newOutputStream(target)){transformer.transform(new DOMSource(output),new StreamResult(out));}
        }
    }

    static class NativeState {
        final org.w3c.dom.Document output;
        int tables=0,nested=0,unverified=0;
        NativeState(org.w3c.dom.Document output){this.output=output;}
        void meta(String name,int value){
            Element item=output.createElement("meta");item.setAttribute("name",name);
            item.setAttribute("content",Integer.toString(value));
            output.getElementsByTagName("head").item(0).appendChild(item);
        }
        Node table(Table table,int depth) throws UnsupportedStructure {
            if(depth>16)throw new UnsupportedStructure("Table nesting exceeds 16 levels");
            if(table.numRows()==0){
                // Damaged/missing PAPX may mark prose as a table without any row bounds.
                // Preserve every paragraph; do not invent cells or discard the whole document.
                unverified++;
                Element segment=output.createElement("div");
                segment.setAttribute("class","briefly-unverified-table-segment");
                // Child grids inside a segment with no parent row bounds are not trustworthy.
                // Keep their source text once, without manufacturing parent-cell ownership.
                for(int p=0;p<table.numParagraphs();p++)appendParagraph(segment,table.getParagraph(p));
                return segment;
            }
            tables++;if(depth>0)nested++;
                    TreeSet<Integer> grid=new TreeSet<>();
                    for(int r=0;r<table.numRows();r++)for(int c=0;c<table.getRow(r).numCells();c++) {
                        TableCell cell=table.getRow(r).getCell(c);
                        grid.add(cell.getLeftEdge());grid.add(cell.getLeftEdge()+cell.getWidth());
                    }
                    Element replacement=output.createElement("table");
                    replacement.setAttribute("data-briefly-native-level",Integer.toString(table.getTableLevel()));
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
                            appendCell(td,cell,table.getTableLevel(),depth);
                            tr.appendChild(td);
                        }
                    }
            return replacement;
        }
        void appendParagraph(Element target,Paragraph paragraph) throws UnsupportedStructure {
            Element p=output.createElement("p");p.setTextContent(cellText(paragraph.text()));
            target.appendChild(p);target.appendChild(output.createTextNode("\n"));
        }
        void appendCell(Element target,TableCell cell,int parentLevel,int depth) throws UnsupportedStructure {
            appendRange(target,cell,parentLevel,depth);
        }
        void appendRange(Element target,Range range,int parentLevel,int depth) throws UnsupportedStructure {
            for(int p=0;p<range.numParagraphs();p++){
                Paragraph paragraph=range.getParagraph(p);
                if(paragraph.isInTable()&&paragraph.getTableLevel()>parentLevel){
                    Table child=NativeTableAccess.at(range,p,parentLevel+1);
                    if(child.getStartOffset()<range.getStartOffset()||child.getEndOffset()>range.getEndOffset())
                        throw new UnsupportedStructure("Nested table exceeds its source cell");
                    target.appendChild(table(child,depth+1));
                    while(p+1<range.numParagraphs()&&range.getParagraph(p+1).getStartOffset()<child.getEndOffset())p++;
                } else appendParagraph(target,paragraph);
            }
        }
    }

    static String cellText(String raw) throws UnsupportedStructure {
        String text=org.apache.poi.hwpf.extractor.WordExtractor.stripFields(raw);
        StringBuilder clean=new StringBuilder();
        for(int i=0;i<text.length();) {
            int cp=text.codePointAt(i);i+=Character.charCount(cp);
            // Word cell/paragraph/manual-line/page breaks are boundaries, not XML text controls.
            if(cp==7||cp==11||cp==12||cp==13)clean.append(' ');
            // Residual Word field delimiters may survive stripFields; retain the field result text.
            else if(cp==19||cp==20||cp==21)continue;
            // Inline picture/drawing anchors are not text; counts produce a visible no-OCR warning.
            else if(cp==1||cp==8)continue;
            else if(cp==30)clean.append('\u2011');
            else if(cp==31)clean.append('\u00ad');
            else if(cp==9||cp==10||(cp>=32&&cp<=0xd7ff)||(cp>=0xe000&&cp<=0xfffd)||(cp>=0x10000&&cp<=0x10ffff))clean.appendCodePoint(cp);
            else throw new UnsupportedStructure("Unrecognized Word cell control character");
        }
        return clean.toString().trim();
    }
}
