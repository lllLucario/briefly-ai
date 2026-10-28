// Local workaround for PAPBinTable.rebuild splitting an original nested-cell PAPX at internal CRs.
// Restore only an exact original interval with explicit table evidence; never scan across prose.
import java.util.*;
import org.apache.poi.hwpf.HWPFDocument;
import org.apache.poi.hwpf.model.*;
import org.apache.poi.hwpf.sprm.*;
import org.apache.poi.util.LittleEndian;

public final class LegacyDocParagraphs {
    private LegacyDocParagraphs() { }
    static boolean explicitNestedCell(PAPX p){
        boolean in=false,level=false,cell=false;
        SprmIterator it=new SprmIterator(p.getGrpprl(),2);
        while(it.hasNext()){
            SprmOperation s=it.next();int op=s.getOperation();
            if(s.getType()!=1)continue;
            if(op==0x16)in=s.getOperand()==1;
            if(op==0x49)level=s.getOperand()>1;
            if(op==0x4b)cell=s.getOperand()==1;
            // A depth delta needs a different proof; leave it unchanged.
            if(op==0x4a)return false;
        }
        return in&&level&&cell;
    }
    static boolean zeroPrms(HWPFDocument doc,int start,int end){
        byte[] table=doc.getTableStream();int at=doc.getFileInformationBlock().getFcClx();
        while(at<table.length&&table[at]==1){if(at+3>table.length)return false;at+=3+LittleEndian.getUShort(table,at+1);}
        if(at+5>table.length||table[at]!=2)return false;
        int size=LittleEndian.getInt(table,at+1);at+=5;
        if(size<4||(size-4)%12!=0||(long)at+size>table.length)return false;
        int n=(size-4)/12;
        for(int i=0;i<n;i++){
            int first=LittleEndian.getInt(table,at+4*i),last=LittleEndian.getInt(table,at+4*(i+1));
            if(first<end&&last>start&&LittleEndian.getUShort(table,at+4*(n+1)+8*i+6)!=0)return false;
        }
        return true;
    }
    static boolean exactSplit(PAPX original,List<PAPX> parts){
        if(parts.size()<2||parts.get(0).getStart()!=original.getStart()||parts.get(parts.size()-1).getEnd()!=original.getEnd())return false;
        int end=original.getStart();
        for(int i=0;i<parts.size();i++){
            PAPX p=parts.get(i);if(p.getStart()!=end)return false;end=p.getEnd();
            if(i==parts.size()-1){if(!Arrays.equals(p.getGrpprl(),original.getGrpprl()))return false;}
            else {
                byte[] empty=p.getGrpprl();
                if(empty.length!=2&&empty.length!=6)return false;
                for(byte b:empty)if(b!=0)return false;
            }
        }
        return true;
    }
    static int restore(HWPFDocument doc)throws java.io.IOException{
        TextPieceTable pieces=new ComplexFileTable(doc.getMainStream(),doc.getTableStream(),doc.getFileInformationBlock().getFcClx(),0).getTextPieceTable();
        List<PAPX> raw=new PAPBinTable(doc.getMainStream(),doc.getTableStream(),doc.getDataStream(),doc.getFileInformationBlock().getFcPlcfbtePapx(),doc.getFileInformationBlock().getLcbPlcfbtePapx(),pieces).getParagraphs();
        raw.sort(Comparator.comparingInt(PAPX::getStart).thenComparingInt(PAPX::getEnd));
        List<PAPX> current=doc.getParagraphTable().getParagraphs(),result=new ArrayList<>();
        int at=0,count=0;
        for(int i=0;i<raw.size();i++){
            PAPX source=raw.get(i);
            // Refuse overlapping original intervals: the correct precedence would need separate evidence.
            if((i>0&&raw.get(i-1).getEnd()>source.getStart())||(i+1<raw.size()&&raw.get(i+1).getStart()<source.getEnd()))continue;
            if(!explicitNestedCell(source)||!zeroPrms(doc,source.getStart(),source.getEnd()))continue;
            while(at<current.size()&&current.get(at).getEnd()<=source.getStart())result.add(current.get(at++));
            int last=at;while(last<current.size()&&current.get(last).getStart()<source.getEnd())last++;
            if(exactSplit(source,current.subList(at,last))){result.add(source);at=last;count++;}
        }
        while(at<current.size())result.add(current.get(at++));
        if(count>0){current.clear();current.addAll(result);}
        return count;
    }
}
