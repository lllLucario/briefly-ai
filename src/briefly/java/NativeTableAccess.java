package org.apache.poi.hwpf.usermodel;

// Package-local constructor bridge: an outer table may start with a nested paragraph.
// Bounds and level come from the source paragraph flags; no text/empty-cell inference.
public final class NativeTableAccess {
    private NativeTableAccess() { }
    public static Table at(Range range,int paragraphIndex,int level) {
        Paragraph first=range.getParagraph(paragraphIndex);
        int last=paragraphIndex;
        while(last+1<range.numParagraphs()){
            Paragraph next=range.getParagraph(last+1);
            if(!next.isInTable()||next.getTableLevel()<level)break;
            last++;
        }
        return new Table(first.getStartOffset(),range.getParagraph(last).getEndOffset(),range,level);
    }
}
