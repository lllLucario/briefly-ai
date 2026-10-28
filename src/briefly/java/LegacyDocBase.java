// Isolated .doc parser experiment. Does not execute macros or parse embedded files.
import java.io.*;
import java.nio.charset.StandardCharsets;
import java.nio.file.*;
import org.apache.tika.extractor.EmbeddedDocumentExtractor;
import org.apache.tika.io.TikaInputStream;
import org.apache.tika.metadata.Metadata;
import org.apache.tika.metadata.TikaCoreProperties;
import org.apache.tika.parser.ParseContext;
import org.apache.tika.parser.microsoft.OfficeParser;
import org.apache.tika.parser.microsoft.OfficeParserConfig;
import org.apache.tika.sax.ToXMLContentHandler;
import org.xml.sax.ContentHandler;
import org.xml.sax.SAXException;

public class LegacyDocBase {
    public static void main(String[] args) throws Exception {
        parse(args,true);
    }
    static void parse(String[] args,boolean includeTextboxes) throws Exception {
        Path output = Path.of(args[0]);
        Files.createDirectories(output);
        OfficeParser parser = new OfficeParser();
        OfficeParserConfig config = new OfficeParserConfig();
        config.setIncludeHeadersAndFooters(false);
        config.setExtractMacros(false);
        config.setIncludeShapeBasedContent(includeTextboxes);
        ParseContext context = new ParseContext();
        context.set(OfficeParserConfig.class, config);
        context.set(EmbeddedDocumentExtractor.class, new EmbeddedDocumentExtractor() {
            public boolean shouldParseEmbedded(Metadata metadata) { return false; }
            public void parseEmbedded(InputStream in, ContentHandler handler, Metadata metadata,
                    boolean outputHtml) throws SAXException, IOException { }
        });
        try (BufferedWriter times = Files.newBufferedWriter(output.resolve("times.tsv"), StandardCharsets.UTF_8)) {
            for (int i = 1; i < args.length; i++) {
                Metadata metadata = new Metadata();
                metadata.set(TikaCoreProperties.RESOURCE_NAME_KEY, Path.of(args[i]).getFileName().toString());
                long start = System.nanoTime();
                try (TikaInputStream in = TikaInputStream.get(Path.of(args[i]));
                     OutputStream out = Files.newOutputStream(output.resolve((i-1) + ".xhtml"))) {
                    parser.parse(in, new ToXMLContentHandler(out, "UTF-8"), metadata, context);
                }
                times.write((i-1) + "\t" + (System.nanoTime()-start)/1000000.0 + "\n");
                times.flush();
            }
        }
    }
}
