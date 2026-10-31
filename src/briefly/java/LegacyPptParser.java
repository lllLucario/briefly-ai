// Legacy .ppt (POI HSLF) to per-slide JSON in visual reading order; errors isolated per input, no text in logs.
import java.awt.geom.Rectangle2D;
import java.io.*;
import java.nio.charset.StandardCharsets;
import java.nio.file.*;
import java.util.*;
import org.apache.poi.ddf.EscherComplexProperty;
import org.apache.poi.ddf.EscherProperty;
import org.apache.poi.ddf.EscherPropertyTypes;
import org.apache.poi.hslf.exceptions.EncryptedPowerPointFileException;
import org.apache.poi.hslf.exceptions.OldPowerPointFormatException;
import org.apache.poi.hslf.usermodel.*;
import org.apache.poi.sl.usermodel.Placeholder;

public class LegacyPptParser {
    static final double ROW_TOLERANCE = 3.6; // points; Docling's pptx backend uses 45720 EMU

    public static void main(String[] args) throws Exception {
        Path root = Path.of(args[0]);
        Files.createDirectories(root);
        for (int i = 1; i < args.length; i++) {
            Path output = root.resolve(Integer.toString(i - 1));
            Files.createDirectories(output);
            try (InputStream in = Files.newInputStream(Path.of(args[i])); HSLFSlideShow show = new HSLFSlideShow(in)) {
                StringBuilder json = new StringBuilder("{\"slides\":[");
                List<HSLFSlide> slides = show.getSlides();
                for (int s = 0; s < slides.size(); s++) {
                    if (s > 0) json.append(',');
                    slide(slides.get(s), json);
                }
                Files.writeString(output.resolve("slides.json"), json.append("]}"), StandardCharsets.UTF_8);
            } catch (EncryptedPowerPointFileException | OldPowerPointFormatException ex) {
                Files.writeString(output.resolve("error.code"), "unsupported_structure", StandardCharsets.UTF_8);
            } catch (Exception | StackOverflowError ex) {
                Files.writeString(output.resolve("error.code"), "parse_failed", StandardCharsets.UTF_8);
            }
        }
    }

    static void slide(HSLFSlide slide, StringBuilder json) {
        List<String> items = new ArrayList<>();
        int[] pictures = {0};
        for (HSLFShape shape : ordered(slide.getShapes())) shape(shape, items, pictures);
        List<String> notes = new ArrayList<>();
        HSLFNotes note = slide.getNotes();
        if (note != null) {
            for (HSLFShape shape : note.getShapes()) {
                if (shape instanceof HSLFTextShape text && text.getPlaceholder() == Placeholder.BODY) {
                    String value = text.getText();
                    if (value != null && !value.isBlank()) notes.add(quote(value.replace('\r', '\n').replace('\u000b', '\n').strip()));
                }
            }
        }
        json.append("{\"hidden\":").append(slide.isHidden()).append(",\"pictures\":").append(pictures[0])
            .append(",\"items\":[").append(String.join(",", items)).append("],\"notes\":[")
            .append(String.join(",", notes)).append("]}");
    }

    /** Top-to-bottom rows (within tolerance of the previous shape), left-to-right inside a row. */
    static List<HSLFShape> ordered(List<HSLFShape> shapes) {
        Map<HSLFShape, Integer> index = new IdentityHashMap<>();
        for (int i = 0; i < shapes.size(); i++) index.put(shapes.get(i), i);
        List<HSLFShape> sorted = new ArrayList<>(shapes);
        sorted.sort(Comparator.comparingDouble((HSLFShape s) -> position(s, true)).thenComparingInt(index::get));
        Comparator<HSLFShape> across = Comparator.comparingDouble((HSLFShape s) -> position(s, false)).thenComparingInt(index::get);
        List<HSLFShape> result = new ArrayList<>(), row = new ArrayList<>();
        double previous = Double.NaN;
        for (HSLFShape shape : sorted) {
            double top = position(shape, true);
            if (!row.isEmpty() && top - previous > ROW_TOLERANCE) {
                row.sort(across);
                result.addAll(row);
                row.clear();
            }
            row.add(shape);
            previous = top;
        }
        row.sort(across);
        result.addAll(row);
        return result;
    }

    static double position(HSLFShape shape, boolean top) {
        try {
            Rectangle2D anchor = shape.getAnchor();
            return anchor == null ? Double.MAX_VALUE : top ? anchor.getY() : anchor.getX();
        } catch (RuntimeException ex) {
            return Double.MAX_VALUE;
        }
    }

    static void shape(HSLFShape shape, List<String> items, int[] pictures) {
        if (shape instanceof HSLFTable table) {
            StringBuilder rows = new StringBuilder("{\"t\":\"table\",\"rows\":[");
            for (int r = 0; r < table.getNumberOfRows(); r++) {
                rows.append(r > 0 ? "," : "").append('[');
                for (int c = 0; c < table.getNumberOfColumns(); c++) {
                    HSLFTableCell cell = table.getCell(r, c);
                    String text = cell == null || cell.getText() == null ? "" : cell.getText();
                    int rowSpan = cell == null ? 1 : Math.max(1, cell.getRowSpan());
                    int colSpan = cell == null ? 1 : Math.max(1, cell.getGridSpan());
                    rows.append(c > 0 ? "," : "").append("{\"x\":").append(quote(clean(text)))
                        .append(",\"rs\":").append(rowSpan).append(",\"cs\":").append(colSpan).append('}');
                }
                rows.append(']');
            }
            items.add(rows.append("]}").toString());
        } else if (shape instanceof HSLFGroupShape group) {
            for (HSLFShape child : ordered(group.getShapes())) shape(child, items, pictures);
        } else if (shape instanceof HSLFPictureShape) {
            pictures[0]++;  // includes embedded OLE objects; their content is not read
        } else if (shape instanceof HSLFTextShape text) {
            Placeholder placeholder = text.getPlaceholder();
            if (placeholder == Placeholder.FOOTER || placeholder == Placeholder.DATETIME || placeholder == Placeholder.SLIDE_NUMBER) return;
            boolean title = placeholder == Placeholder.TITLE || placeholder == Placeholder.CENTERED_TITLE;
            boolean any = false;
            for (HSLFTextParagraph paragraph : text.getTextParagraphs()) {
                StringBuilder value = new StringBuilder();
                for (HSLFTextRun run : paragraph.getTextRuns()) value.append(run.getRawText());
                String line = clean(value.toString());
                if (line.isEmpty()) continue;
                any = true;
                if (line.equals("*")) continue;  // a lone slide-number field
                boolean numbered = !title && paragraph.getAutoNumberingScheme() != null;
                boolean bullet = !title && (numbered || paragraph.isBullet());
                Integer start = numbered ? paragraph.getAutoNumberingStartAt() : null;
                items.add("{\"t\":" + quote(title ? "title" : bullet ? "li" : "p") + ",\"x\":" + quote(line)
                    + ",\"lv\":" + paragraph.getIndentLevel() + ",\"num\":" + numbered + ",\"start\":" + (start == null ? 1 : start) + "}");
            }
            if (!any) wordArt(text, items);
        } else if (shape instanceof HSLFSimpleShape simple) {
            wordArt(simple, items);
        }
    }

    /** WordArt keeps its text in the shape's geometry-text property rather than a text body. */
    static void wordArt(HSLFSimpleShape shape, List<String> items) {
        EscherProperty property = shape.getEscherOptRecord() == null ? null
            : shape.getEscherOptRecord().lookup(EscherPropertyTypes.GEOTEXT__UNICODE);
        if (!(property instanceof EscherComplexProperty complex)) return;
        String text = clean(new String(complex.getComplexData(), StandardCharsets.UTF_16LE).replace("\0", ""));
        if (!text.isEmpty()) items.add("{\"t\":\"p\",\"x\":" + quote(text) + ",\"lv\":0,\"num\":false,\"start\":1}");
    }

    /** Line breaks inside a paragraph become spaces, as in Docling's pptx backend. */
    static String clean(String text) {
        return text.replace('\r', ' ').replace('\n', ' ').replace('\u000b', ' ').strip();
    }

    static String quote(String text) {
        StringBuilder out = new StringBuilder("\"");
        for (char c : text.toCharArray()) {
            switch (c) {
                case '"' -> out.append("\\\"");
                case '\\' -> out.append("\\\\");
                case '\n' -> out.append("\\n");
                case '\t' -> out.append("\\t");
                default -> {
                    if (c < 0x20) out.append(String.format("\\u%04x", (int) c));
                    else out.append(c);
                }
            }
        }
        return out.append('"').toString();
    }
}
