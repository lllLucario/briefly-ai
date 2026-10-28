// Native main-textbox source ranges. Normalize known cell boundaries before XHTML sanitization.
import java.util.*;
import org.apache.poi.hwpf.HWPFDocument;
import org.apache.poi.hwpf.model.FIBFieldHandler;
import org.apache.poi.hwpf.usermodel.Range;
import org.apache.poi.util.LittleEndian;
import org.w3c.dom.*;

public final class LegacyDocTextboxes {
    private LegacyDocTextboxes() { }

    static List<int[]> ranges(HWPFDocument doc,Range story){
        byte[] main=doc.getMainStream(),table=doc.getTableStream();
        // Locate FibRgFcLcb from the declared variable-size FIB groups, not an assumed fixed offset.
        int offset=32;
        if(main.length<offset+2)return null;
        int csw=LittleEndian.getUShort(main,offset);offset+=2+csw*2;
        if(offset<0||offset+2>main.length)return null;
        int cslw=LittleEndian.getUShort(main,offset);offset+=2+cslw*4;
        if(offset<0||offset+2>main.length)return null;
        int count=LittleEndian.getUShort(main,offset);offset+=2;
        int field=FIBFieldHandler.PLCFTXBXTXT;
        if(count<=field||offset+(long)(field+1)*8>main.length)return null;
        int fc=LittleEndian.getInt(main,offset+field*8),length=LittleEndian.getInt(main,offset+field*8+4);
        // PlcftxbxTxt: (n+1) CPs plus n 22-byte FTXBXS records, including a final ignored range.
        if(fc<0||length<30||(length-4)%26!=0||(long)fc+length>table.length)return null;
        int n=(length-4)/26;
        if(n<2||n>100000)return null;
        List<int[]> result=new ArrayList<>();int last=0,storyLength=story.getEndOffset()-story.getStartOffset();
        for(int i=0;i<n-1;i++){
            int start=LittleEndian.getInt(table,fc+4*i),end=LittleEndian.getInt(table,fc+4*(i+1));
            if(start!=last||start<0||end<=start||end>storyLength)return null;
            result.add(new int[]{start,end});last=end;
        }
        String tail=story.text().substring(last);
        if(!tail.chars().allMatch(c->c==13||c==10||c==0))return null;
        return result;
    }

    static int appendGroup(Document output,Element parent,String raw,int start,int group){
        Element box=output.createElement("div");box.setAttribute("class","briefly-textbox");
        box.setAttribute("data-source-story","main-textbox");box.setAttribute("data-source-group",Integer.toString(group));
        box.setAttribute("data-source-start",Integer.toString(start));box.setAttribute("data-source-end",Integer.toString(start+raw.length()));
        parent.appendChild(box);
        int segment=0,cell=0,marks=0;
        for(int i=0;i<=raw.length();i++){
            char boundary=i<raw.length()?raw.charAt(i):0;
            if(i<raw.length()&&boundary!=7&&boundary!=13&&boundary!=10&&boundary!=11&&boundary!=12)continue;
            Element p=output.createElement("p");p.setAttribute("data-source-cell",Integer.toString(cell));
            p.setAttribute("data-source-start",Integer.toString(start+segment));p.setAttribute("data-source-end",Integer.toString(start+i));
            if(boundary==7){p.setAttribute("data-source-cell-end","true");marks++;}
            // Segment has no cell/paragraph marker now. Keep legitimate U+FFFD and all other content.
            try{p.setTextContent(LegacyDocStructured.cellText(raw.substring(segment,i)));}
            catch(LegacyDocStructured.UnsupportedStructure error){throw new IllegalArgumentException("Unsupported textbox control",error);}
            box.appendChild(p);box.appendChild(output.createTextNode("\n"));
            if(boundary==7)cell++;
            segment=i+1;
        }
        return marks;
    }

    static void append(Document output,HWPFDocument doc,LegacyDocStructured.NativeState state){
        Range story=doc.getMainTextboxRange();String raw=story.text();
        if(raw.isBlank())return;
        Element appendix=output.createElement("div");appendix.setAttribute("class","briefly-textboxes");
        appendix.setAttribute("data-source-story","main-textbox");
        Element label=output.createElement("h1");label.setAttribute("data-parser-generated","true");
        label.setTextContent("文本框附录（原文正文位置未绑定）");appendix.appendChild(label);
        output.getElementsByTagName("body").item(0).appendChild(appendix);
        List<int[]> groups=ranges(doc,story);int marks=0,unbound=0;
        if(groups==null){groups=List.of(new int[]{0,raw.length()});unbound=1;}
        int index=0;
        for(int[] range:groups)marks+=appendGroup(output,appendix,raw.substring(range[0],range[1]),story.getStartOffset()+range[0],++index);
        state.meta("briefly:textbox-ranges",groups.size());state.meta("briefly:textbox-cell-boundaries",marks);
        state.meta("briefly:textbox-range-boundaries-unverified",unbound);
    }
}
