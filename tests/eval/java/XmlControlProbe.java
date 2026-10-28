import java.io.*;
import java.nio.file.*;
import org.apache.tika.sax.ToXMLContentHandler;
import org.apache.tika.sax.XHTMLContentHandler;
import org.apache.tika.metadata.Metadata;
import org.xml.sax.helpers.AttributesImpl;

public class XmlControlProbe {
    public static void main(String[] args)throws Exception{
        try(OutputStream out=Files.newOutputStream(Path.of(args[0]))){
            ToXMLContentHandler handler=new ToXMLContentHandler(out,"UTF-8");
            handler.startDocument();handler.startElement("","p","p",new AttributesImpl());
            char[] text="甲\u0007乙".toCharArray();handler.characters(text,0,text.length);
            handler.endElement("","p","p");handler.endDocument();
        }
        try(OutputStream out=Files.newOutputStream(Path.of(args[1]))){
            XHTMLContentHandler handler=new XHTMLContentHandler(new ToXMLContentHandler(out,"UTF-8"),new Metadata());
            handler.startDocument();handler.element("p","甲\u0007乙");handler.endDocument();
        }
    }
}
