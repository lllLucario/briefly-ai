// Original synthetic content: source cell identities, empty neighbors, literal replacement char.
import java.nio.file.*;
import javax.xml.parsers.DocumentBuilderFactory;
import javax.xml.transform.*;
import javax.xml.transform.dom.DOMSource;
import javax.xml.transform.stream.StreamResult;
import org.w3c.dom.*;

public class TextboxBoundaryProbe {
    static void check(boolean condition){if(!condition)throw new AssertionError("Textbox boundary regression");}
    public static void main(String[] args)throws Exception{
        Document doc=DocumentBuilderFactory.newInstance().newDocumentBuilder().newDocument();
        Element html=doc.createElement("html");doc.appendChild(html);Element body=doc.createElement("body");html.appendChild(body);
        Element boxes=doc.createElement("div");boxes.setAttribute("class","briefly-textboxes");body.appendChild(boxes);
        int marks=LegacyDocTextboxes.appendGroup(doc,boxes,"标签甲\u0007\u0007标签乙\u0007真实�文本\r",100,1);
        LegacyDocTextboxes.appendGroup(doc,boxes,"隔壁标签\u0007",200,2);
        check(marks==3);
        Element first=(Element)boxes.getFirstChild();NodeList cells=first.getElementsByTagName("p");
        check(cells.item(0).getTextContent().equals("标签甲"));
        check(cells.item(1).getTextContent().isEmpty());
        check(cells.item(2).getTextContent().equals("标签乙"));
        check(cells.item(3).getTextContent().equals("真实�文本"));
        check(((Element)cells.item(2)).getAttribute("data-source-cell").equals("2"));
        check(((Element)cells.item(0)).getAttribute("data-source-cell-end").equals("true"));
        check(((Element)boxes.getLastChild()).getAttribute("data-source-start").equals("200"));
        Transformer t=TransformerFactory.newInstance().newTransformer();t.transform(new DOMSource(doc),new StreamResult(Path.of(args[0]).toFile()));
        System.out.println("8 textbox boundary/ownership checks passed");
    }
}
